"""Client-side transports that ship log records to the central log server, with durable resume-by-id replay."""

# Standard library imports
import asyncio
import base64
import functools
import logging
import queue as _queue_mod
import select
import selectors
import socket
import threading
from contextlib import suppress
from logging.handlers import DEFAULT_TCP_LOGGING_PORT, SocketHandler
from time import monotonic, time
from typing import TYPE_CHECKING, Any, Literal, Self, override

# Third party imports
import cloudpickle
import orjson

# First party imports
from aeth_ext.central_log_server.client.durability import RecordDurability
from aeth_ext.central_log_server.client.emergency import EmergencyModeTracker
from aeth_ext.central_log_server.client.filters import RemoteReachability
from aeth_ext.central_log_server.protocol import (
  LENGTH_STRUCT,
  ApplyFailure,
  ApplySuccess,
  ClientHandshake,
  HandshakeAck,
  decode_server_message,
  encode_json_packet,
  record_to_payload,
)
from aeth_ext.errors import alert, shutdown, trigger_shutdown
from aeth_ext.logging.emergency_diagnostics import emergency_diagnostic
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
  from aeth_ext.central_log_server.client.history import HistoryEntry
  from aeth_ext.errors.exception_trail import ExceptionTrail
  from aeth_ext.logging.bases import TaggedLogRecord


settings = BaseSettings.get_settings()
logger = logging.getLogger(__name__)

# How long a client's teardown waits on its own background work: a coroutine handed back
# to its event loop, or a drain or sender thread's last pass. Comfortably inside the
# threaded shutdown pass's budget, so one wedged client cannot consume the whole grace period.
_LOOP_TEARDOWN_TIMEOUT = 3.0

# The apply-result watcher waits in slices this long, then checks its socket is still live. On Linux,
# ``_drop_socket``'s ``close`` can unregister the fd from the watcher's epoll before it collects the
# shutdown's wake-up, and an unbounded wait would then never return.
_WATCH_SLICE = 1.0


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
  """Read exactly *size* bytes from *sock*, or ``None`` if the peer closed early."""
  chunks = bytearray()
  while len(chunks) < size:
    chunk = sock.recv(size - len(chunks))
    if not chunk:
      return None
    chunks.extend(chunk)
  return bytes(chunks)


def _drop_socket(sock: socket.socket) -> None:
  """Shut *sock* down, then close it: on Linux a bare ``close`` doesn't wake the apply-result watcher waiting on it.

  The shutdown's wake-up can still be lost to the close; ``_WATCH_SLICE`` bounds the watcher's wait for that case.
  """
  with suppress(OSError):
    sock.shutdown(socket.SHUT_RDWR)
  with suppress(OSError):
    sock.close()


def read_server_message_sync(sock: socket.socket, timeout: float | None = 5.0) -> HandshakeAck | ApplySuccess | ApplyFailure | None:
  """Best-effort read of one server message (D-E5), or ``None`` if malformed/absent/timed out.

  A failure here (timeout, malformed payload, or the connection dying) is not fatal to the connection
  itself - the caller decides what that means for its own purposes - so the socket is left untouched on
  any error; a genuinely broken socket will surface the next time a record is actually sent.
  ``ApplySuccess`` is returned for ``HandshakeSocketHandler``'s apply-result watcher, which re-arms the
  rejection alert on it.

  *timeout* is set on *sock* for the read and the previous one restored after; pass ``sock.gettimeout()``
  to leave a socket another thread is using as it is. The restore is wrapped in ``suppress(OSError)``: from a
  background watcher thread, *sock* can be closed concurrently by whichever thread owns it (e.g. a send
  failure or ``close()``), racing this cleanup step after ``recv`` already returned.
  """
  previous_timeout = sock.gettimeout()
  try:
    sock.settimeout(timeout)  # raises if the owner already closed *sock*
    header = _recv_exact(sock, LENGTH_STRUCT.size)
    if header is None:
      return None
    (length,) = LENGTH_STRUCT.unpack(header)
    payload = _recv_exact(sock, length)
    if payload is None:
      return None
    return decode_server_message(payload)
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


# D-E7: the message every ack-read-failure site below alerts with. Fixed rather than
# per-call, since every client's ack-reading path fails the same way (timeout, malformed
# payload, dead connection) and there is nothing call-site-specific to report.
_ACK_READ_FAILURE_REASON = (
  "the client could not read the central log server's handshake acknowledgement (timeout, "
  "malformed payload, or a dead connection). Resume-by-id is skipped for this reconnect; logging "
  "otherwise continues normally, live from this point."
)


# Renders a record diverted by `HandshakeSocketHandler.emit`'s re-entrancy guard; the stdlib
# formatter appends exc_info and stack_info, so nothing about the record is lost.
_DIVERTED_RECORD_FORMATTER = logging.Formatter("%(name)s %(levelname)s: %(message)s")


class HandshakeSocketHandler(SocketHandler, RecordDurability, EmergencyModeTracker):
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

  .. note::
     ``flush()`` on this class resolves to :class:`logging.Handler`'s no-op stub, not
     :meth:`~aeth_ext.central_log_server.client.durability.RecordDurability.flush` - ``SocketHandler``
     precedes ``RecordDurability``/``EmergencyModeTracker`` in the MRO and does not override ``flush``, so
     it wins the lookup. Callers needing a real history flush must call ``RecordDurability.flush(self)``
     explicitly (see :meth:`close`, which does exactly this).

  The server replies with a
  :class:`~aeth_ext.central_log_server.protocol.HandshakeAck`: a rejection
  (invalid config) shuts the process down unless ``shutdown_on_rejection=False``,
  while a successful ack carries resume information used to replay any backlog
  the server is missing.

  **Delivery never runs on the logging thread.** ``emit`` assigns the record an id, appends it to
  history and rings a doorbell; history is the queue, there is no second one. A daemon sender
  thread (``_run``) owns the socket: it connects with stdlib's backoff, which also spaces reconnects
  after a dropped connection, handshakes, replays what the ack says the server is missing, then
  sends every entry after ``last_sent_id`` in id order.
  The server doesn't dedupe, so nothing is sent twice: ``last_sent_id`` only moves back to replay
  what the server's ack says never arrived.

  **Nothing on the delivery path may log**: not ``emit``, the sender or the apply-result watcher,
  except ``trigger_shutdown``'s one ``critical`` on a default-mode rejection, as the process goes down.
  This handler normally sits on the root logger, so such a call comes straight back into it: on
  ``emit``'s own thread re-entrantly (``Handler.lock`` is an ``RLock``), which with a dead socket
  once recursed until ``RecursionError`` and reported one fatal shutdown per unwinding frame (the
  2026-08-27 production hang), or on the sender as one more record to fail on and report again.
  Failures go to ``emergency_diagnostic`` (stderr plus the persisted emergency-diagnostics file)
  instead, and ``emit`` diverts any record that reaches it re-entrantly from the thread already
  inside it to that same channel, rendered in full, traceback included, so a future slip lands in
  the emergency-diagnostics file instead of hanging the process.

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
    shutdown_on_rejection: bool = True,
  ) -> None:
    """See the class docstring for the common parameters.

    Args:
        program_name: Identity sent in the handshake; also names this program's
            history directory on the server and client.
        config: Dict-based logging configuration the server applies into this
            program's private hierarchy. Must already be fully resolved
            client-side except for server-side prefixes (``logdir://``,
            ``cfg://``, ``ext://``) and ``definition`` payloads.
        host: Log server hostname.
        port: Log server TCP port.
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
        shutdown_on_rejection: Shut down when the server rejects the config,
            at the handshake or when applying it. ``False`` instead alerts once
            until the next ``ApplySuccess``, drops the connection, keeps the
            records in history, and reconnects with backoff.
    """
    SocketHandler.__init__(self, host, port)
    RecordDurability.__init__(
      self,
      program_name,
      max_history_records,
      max_history_bytes,
      max_history_age,
      id_checkpoint_backend=id_checkpoint_backend,
      event_loop=event_loop,
    )
    self._first_id = self._next_id  # the first id this process assigns; lower ones are earlier processes'
    EmergencyModeTracker.__init__(
      self,
      self.history_dir,
      program_name,
      time_threshold=emergency_time_threshold * 60.0,
      attempt_threshold=emergency_attempt_threshold,
    )
    self._config: dict[str, Any] = dict(config)
    self._reachability = RemoteReachability(self._config)
    self._handshake_rejected: str | None = None
    # Per-thread re-entrancy flag for ``emit`` -- see the class docstring.
    self._emit_guard = threading.local()
    # ``emit``'s wake-up for the sender. ``SimpleQueue.put`` is the one that's safe inside a signal handler:
    # ``Queue.put`` and ``Event.set`` take locks the interrupted code may hold.
    self._doorbell: _queue_mod.SimpleQueue[None] = _queue_mod.SimpleQueue()
    # Guards ``sock`` and connection setup between the sender and ``connect_and_verify``. ``emit`` and ``close`` never take it.
    self._sock_lock = threading.Lock()
    self._stop = threading.Event()
    # ``find_after``'s hint: no entry after ``last_sent_id`` was created much before this.
    self._last_sent_created: float | None = None
    self._ack_failure_alerted = False  # one alert per outage; the next ack that reads clears it
    self._gap_reported_after: int | None = None  # the ``last_sent_id`` whose history gap was last diagnosed
    self._shutdown_on_rejection = shutdown_on_rejection
    # The opt-out's state, shared by the handshaking thread and the apply-result watcher.
    self._rejection_lock = threading.Lock()
    self._rejection_alerted = False  # cleared by an ``ApplySuccess``: an ok ack can still be followed by an ``ApplyFailure``
    self._rejection_period: float | None = None  # backoff after the latest of consecutive rejections
    # Backoff after the latest of consecutive drops of a connected socket; under ``_sock_lock``, reset by a pass that sends.
    self._drop_period: float | None = None
    # Applied by the sender's next ``createSocket``: the watcher must not write ``retryTime`` under the sender.
    self._pending_backoff: float | None = None
    self.retryPeriod = self.retryStart  # stdlib assigns it only on a failed connect; ``createSocket`` applies the backoff to it

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )
    # Started last, so a handler that never sees ``connect_and_verify`` still delivers.
    self._sender = threading.Thread(target=self._run, name=f"log-sender-{program_name}", daemon=True)
    self._sender.start()

  def _arm_shutdown(self, trails: tuple[ExceptionTrail, ...]) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see ``ShutdownPhase.INTERRUPT`` for the rules this obeys.
    """
    self.arm_shutdown()

  def _finish_shutdown(self, trails: tuple[ExceptionTrail, ...]) -> None:
    """Threaded-phase teardown (D-I8): a shutdown-registry adapter for ``close``.

    Not ``self.close`` registered directly -- ``close`` overrides
    ``logging.Handler.close``, whose zero-arg signature ``logging.shutdown()`` also calls
    into, so it cannot itself take the trail-tuple argument every shutdown callback now
    requires.
    """
    self.close()

  # Budget for one blocking send once connected. stdlib ``makeSocket`` leaves its 1s *connect*
  # timeout on the socket, under which a backlog replay against a briefly stalled server
  # silently drops the connection (and, with the server's unread ``ApplySuccess`` still in the
  # receive buffer, the close goes out as an RST rather than a FIN).
  SEND_TIMEOUT: float = 30.0

  @override
  def makeSocket(self, timeout: float = 1) -> socket.socket:
    sock = super().makeSocket(timeout)
    sock.settimeout(self.SEND_TIMEOUT)
    return sock

  @override
  def createSocket(self) -> None:
    with self._rejection_lock:
      backoff, self._pending_backoff = self._pending_backoff, None
    if backoff is not None:
      # stdlib's own schedule: a later connect failure keeps doubling from here.
      self.retryPeriod = backoff
      self.retryTime = time() + backoff
    previous_sock = self.sock
    super().createSocket()
    # ``super().createSocket()`` only assigns a new object to ``self.sock`` when
    # a fresh connection is actually established, so this guard fires exactly
    # once per (re)connection - the moment we must announce ourselves.
    sock = self.sock
    if sock is not None and sock is not previous_sock:
      try:
        self._send_handshake()
      except BaseException:
        # A handshake that raised partway left a socket its replay never ran on. Sending on it would look up from a
        # stale ``last_sent_id`` (0 on a first connect: all retained history), so drop it and the next pass redoes the
        # handshake. Here rather than in the callers: both hold ``_sock_lock`` already.
        _drop_socket(sock)
        if self.sock is sock:
          self.sock = None
        self._back_off_after_drop()
        raise

  def connect_and_verify(self) -> None:
    """Connect on the caller's thread and fail fast if the server rejects our remote config.

    The sender connects on its own; startup code calls this so a rejected handshake surfaces as a
    ``RuntimeError`` now instead of records silently going nowhere. Whichever of the two runs first
    connects and the other finds the socket; a rejection the sender saw first stays in
    ``_handshake_rejected`` until the next handshake, so it's acted on without handshaking again (a
    second rejection would alert twice and block startup on its send deadline), and checked before
    releasing the lock: the sender's next handshake would reset it. A merely unreachable server is not
    an error (the handler buffers and retries); only an explicit rejection raises.

    Raises:
        RuntimeError: The server rejected the handshake and ``shutdown_on_rejection`` is set.
    """
    with self._sock_lock:
      if self.sock is None and self._handshake_rejected is None:
        self.createSocket()
      if self._handshake_rejected is not None and self._shutdown_on_rejection:
        raise RuntimeError(f"Central log server rejected the handshake for {self._program_name!r}: {self._handshake_rejected}")

  def _send_handshake(self) -> None:
    """Send the identifying handshake as the very first message on the socket, then act on the server's ack.

    Runs under ``_sock_lock`` on whichever thread connected. A fresh ``ClientHandshake`` is built on every
    call so each (re)connection sends a clean snapshot of the remote config. The ack is read best-effort:

    - A rejection closes the connection, records the server's error (D-E6) and shuts down, or under
      ``shutdown_on_rejection=False`` goes to ``_reject_without_shutdown``.
    - A good ack replays the backlog it says the server is missing. On a first send, it then raises
      ``last_sent_id`` to the acked id, capped at the last id assigned so a server ahead of a wiped id
      checkpoint can't silence live delivery. Left at 0 after an empty backlog, the sender's lookup would
      re-send every earlier process's history.
    - An unreadable ack alerts once per outage (D-E7) and continues after ``last_sent_id``; if nothing was
      sent yet this process, after the ids earlier processes assigned, which the server may already hold.

    Both live outcomes call ``record_success`` before history is read, so the emergency writer has drained
    its queue to disk by then. Both also start a watcher for the D-E2 out-of-band apply result.
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
      # Mirror SocketHandler.send's error handling so the sender reconnects (and re-sends the
      # handshake) instead of streaming records to a server that never received our identity.
      _drop_socket(sock)
      self.sock = None
      self._back_off_after_drop()
      return

    message = read_server_message_sync(sock)
    if not isinstance(message, HandshakeAck):
      self.record_success()
      if self.last_sent_id == 0:
        # The server doesn't dedupe, so earlier processes' records are skipped; this process's can't be there yet.
        # No ``created`` hint: this process's entries after it may sit in files of any age.
        self.mark_sent(self._first_id - 1)
        self._last_sent_created = None
      emergency_diagnostic(
        f"failed to read handshake ack for {self._program_name!r}; resume-by-id skipped, continuing after id {self.last_sent_id}"
      )
      if not self._ack_failure_alerted:
        self._ack_failure_alerted = True
        # Don't hold delivery, and ``_sock_lock``, for the alert's send deadline.
        alert(
          f"Failed to read handshake ack for {self._program_name!r}",
          f"Failed to read handshake ack for {self._program_name!r}: {_ACK_READ_FAILURE_REASON}",
          in_except_block=False,
          wait=False,
        )
    elif not message.ok:
      self._ack_failure_alerted = False
      self._handshake_rejected = message.error or "rejected without a reason"
      emergency_diagnostic(f"central log server rejected the logging config for {self._program_name!r}: {self._handshake_rejected}")
      if self._shutdown_on_rejection:
        trigger_shutdown(
          f"Central log server rejected logging config for {self._program_name!r}",
          f"Central log server rejected the logging config for {self._program_name!r}: {self._handshake_rejected}",
        )
      else:
        self._reject_without_shutdown(self._handshake_rejected)
      _drop_socket(sock)
      self.sock = None
      return
    else:
      self._ack_failure_alerted = False
      self.record_success()
      # Only before a first send: after one, the replay covers an ack within our ids, and an ack past them is a server
      # ahead of us (a reused INSTANCE_ID, a reset volume), where seeding would skip everything recorded since the drop.
      # Capped at earlier processes' ids: none of this process's can be on the server before its first send.
      fresh = self.last_sent_id == 0
      self._send_entries(self.resolve_backlog(message))
      acked = min(message.last_record_id or 0, self._first_id - 1)
      if fresh and acked > self.last_sent_id:
        self.mark_sent(acked)
        self._last_sent_created = message.last_received_at
        # ``resolve_backlog`` has just reported any gap after the acked id; the sender's lookup needn't again.
        self._gap_reported_after = acked
    # An unread ack leaves the socket untouched (see read_server_message_sync), so it's watched either way,
    # as the drainers do on the same fallback path.
    threading.Thread(
      target=self._watch_apply_result,
      args=(sock,),
      name=f"apply-result-watcher-{self._program_name}",
      daemon=True,
    ).start()

  def _watch_apply_result(self, sock: socket.socket) -> None:
    """Short-lived watcher for the D-E2/D-E2a out-of-band apply-result message.

    A thread of its own: it blocks until the result arrives, which neither thread that handshakes (the
    sender, or ``connect_and_verify``'s caller) can wait for. The server always replies (D-E2a), so it
    lives briefly. It waits in a selector and reads under the socket's own timeout, since ``settimeout(None)``
    on the socket the sender writes would let a server that stops reading block those sends forever. A
    socket dropped under it (``_drop_socket``) ends the watch quietly: nothing was rejected, so no alert or backoff.
    It's noticed by the wake-up, or by the check after each ``_WATCH_SLICE`` that the socket is still open and
    still the handler's.
    """
    try:
      try:
        with selectors.DefaultSelector() as selector:  # not ``select.select``, which refuses fds past FD_SETSIZE
          selector.register(sock, selectors.EVENT_READ)
          while not selector.select(timeout=_WATCH_SLICE):
            if sock.fileno() == -1 or self.sock is not sock:
              return
      except OSError, ValueError:  # the sender closed it first
        return
      message = read_server_message_sync(sock, timeout=sock.gettimeout())
      if isinstance(message, ApplySuccess):
        with self._rejection_lock:
          self._rejection_alerted = False
          # Not ``_pending_backoff``: the connect this result came on consumed any rejection's, and a drop's may follow.
          self._rejection_period = None
      elif isinstance(message, ApplyFailure):
        emergency_diagnostic(f"central log server rejected the logging config for {self._program_name!r}: {message.error}")
        if self._shutdown_on_rejection:
          trigger_shutdown(
            f"Central log server rejected logging config for {self._program_name!r}",
            f"Central log server rejected the logging config for {self._program_name!r}: {message.error}",
          )
        else:
          self._reject_without_shutdown(message.error)
          # The sender owns the socket, so its next send meets the shutdown and reconnects as usual.
          with suppress(OSError):  # it may have closed it already
            sock.shutdown(socket.SHUT_RDWR)
    except Exception as e:  # noqa: BLE001 -- under the crash hooks an escape is a fatal shutdown, defeating the opt-out
      emergency_diagnostic(f"apply-result watcher for {self._program_name!r} failed", exc=e)

  def _reject_without_shutdown(self, error: str) -> None:
    """Handle a rejection under ``shutdown_on_rejection=False``: alert once per excursion and back off the reconnect.

    Called by the handshaking thread and the apply-result watcher, hence ``_rejection_lock``; both sit on the delivery
    path, so the alert doesn't wait. The backoff follows stdlib's schedule, ``retryStart`` doubling up to ``retryMax``
    across consecutive rejections: a successful connect clears ``retryTime``, so a server that rejects every
    handshake would otherwise be redialed on every wake.
    """
    with self._rejection_lock:
      alerting = not self._rejection_alerted
      self._rejection_alerted = True
      period = self.retryStart if self._rejection_period is None else min(self._rejection_period * self.retryFactor, self.retryMax)
      self._rejection_period = self._pending_backoff = period
    if alerting:
      alert(
        f"Central log server rejected logging config for {self._program_name!r}",
        f"Central log server rejected the logging config for {self._program_name!r}: {error}. Records stay in history "
        "and the handler keeps reconnecting with backoff; no further alert until the server applies the config.",
        in_except_block=False,
        wait=False,
      )

  def _back_off_after_drop(self) -> None:
    """Back off the reconnect after dropping a connected socket, on ``_reject_without_shutdown``'s schedule.

    Caller holds ``_sock_lock``. A connect that succeeded cleared ``retryTime``, so a server that accepts and then
    resets would otherwise get a full handshake and replay on every pass. The drop and rejection streaks are kept
    apart because their resets differ: an ``ApplySuccess`` follows every good handshake, even one dropped later.
    """
    period = self.retryStart if self._drop_period is None else min(self._drop_period * self.retryFactor, self.retryMax)
    self._drop_period = period
    with self._rejection_lock:
      self._pending_backoff = max(self._pending_backoff or 0.0, period)

  def _send_entries(self, entries: tuple[HistoryEntry, ...]) -> bool:
    """Send *entries* in order, marking each sent: the handshake's replay and the sender's sends.

    Caller holds ``_sock_lock``. An entry that can't be encoded (a ``%`` format its args don't fit, say)
    is reported and skipped, since retrying it would wedge delivery behind it for good.

    Returns:
        ``False`` if there was no socket or a send failed and dropped it.
    """
    sock = self.sock
    if sock is None:
      return False
    for entry in entries:
      try:
        packet = self.makePickle(entry.record)
      except Exception as e:  # noqa: BLE001 -- see the docstring
        emergency_diagnostic(f"skipped record {entry.id} for {self._program_name!r}: it can't be encoded", exc=e)
      else:
        try:
          sock.sendall(packet)
        except OSError as e:
          # Drop the socket *before* reporting, so nothing reported can find a live socket to retry on.
          _drop_socket(sock)
          self.sock = None
          self._back_off_after_drop()
          emergency_diagnostic(f"send to the log server for {self._program_name!r} failed, dropping the connection: {e}")
          return False
      self.mark_sent(entry.id)
      self._last_sent_created = entry.created
    return True

  @override
  def emit(self, record: TaggedLogRecord) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Assign a durable id, append the record to history, and ring the sender's doorbell.

    Records the remote config *provably* cannot deliver anywhere (per
    :class:`~aeth_ext.central_log_server.client.filters.RemoteReachability`'s
    level-only analysis) are dropped up front - they never consume an id,
    enter history, or cross the wire. Nothing else happens here: no socket and no
    emergency-mode bookkeeping, which belong to the sender (see the class docstring).
    """
    if getattr(self._emit_guard, "active", False):
      # Re-entered from this same thread: something on the delivery path logged. Delivering it
      # would recurse (see the class docstring), so the whole record -- message, exc_info and
      # stack_info -- is diverted to the emergency-diagnostics channel instead.
      emergency_diagnostic(
        f"diverted a record logged from inside the transport for {self._program_name!r}: {_DIVERTED_RECORD_FORMATTER.format(record)}"
      )
      return
    self._emit_guard.active = True
    try:
      if record.levelno < self._reachability.threshold_for(record.name):
        return
      self.record(record, local_root=None, emergency_writer=self.writer)
      self._doorbell.put(None)
    except Exception as e:  # noqa: BLE001 -- a handler must never let a failure escape (stdlib handleError contract)
      # Not an error boundary: a logging transport failing is not process-fatal, and routing it
      # through `report_exc` meant one alert and one shutdown request per failure.
      emergency_diagnostic(f"recording a log record for {self._program_name!r} failed", exc=e)
    finally:
      self._emit_guard.active = False

  def _run(self) -> None:
    """Sender thread: sleep on the doorbell, deliver under ``_sock_lock``, and return once ``close`` stops it.

    Wakes on every ring and at least once a second, so a dropped connection is retried at stdlib's
    ``retryTime`` backoff (1 s doubling to 30 s) without spinning. ``pending`` holds from a ring until a
    pass has sent everything, so a connection that comes back delivers what piled up while it was down.
    After ``_stop`` one last pass sends what it can on the socket it has, without reconnecting.
    """
    pending = False
    while True:
      try:
        self._doorbell.get(timeout=1.0)
        pending = True
        while True:
          self._doorbell.get_nowait()
      except _queue_mod.Empty:
        pass
      stopping = self._stop.is_set()
      try:
        with self._sock_lock:
          pending = self._deliver(pending=pending, reconnect=not stopping)
      except Exception as e:  # noqa: BLE001 -- a logging bug must not end delivery, nor turn fatal through the thread hook
        emergency_diagnostic(f"log sender for {self._program_name!r} failed; retrying", exc=e)
        self._stop.wait(1.0)
      if stopping:
        break
    with self._sock_lock:
      if self.sock is not None:
        _drop_socket(self.sock)
        self.sock = None

  def _deliver(self, *, pending: bool, reconnect: bool) -> bool:
    """One sender pass: reconnect if allowed, then send every history entry after ``last_sent_id``, in id order.

    Caller holds ``_sock_lock``. Split from ``_run`` only to keep each under the complexity lint.

    Args:
        pending: Whether a ring since the last complete pass left records to send.
        reconnect: False for the final pass after ``_stop``.

    Returns:
        Whether records are still pending: there was no socket, or a send failed.
    """
    if self.sock is None and reconnect:
      self.createSocket()  # never raises; a fresh socket runs ``_send_handshake`` and its replay
    if self.sock is None:
      if pending and reconnect:
        self.record_failure()
        self.maybe_enter()
      return pending
    if not pending:
      return False
    # 0 means nothing sent yet this process, and ``find_after(0, ...)`` can return ``None`` where everything is meant.
    entries = self.history.find_after(self.last_sent_id or None, self._last_sent_created)
    if entries is None:
      # The successors of ``last_sent_id`` are gone: aged out, or lost before reaching disk. One full read
      # resumes from the oldest retained entry after it; once that is sent, lookups place again.
      gap_after = self.last_sent_id
      entries = tuple(e for e in self.history.find_after(None, None) or () if e.id > gap_after)
      if self._gap_reported_after != gap_after:
        self._gap_reported_after = gap_after
        resume = f"resuming from id {entries[0].id}" if entries else "nothing after it is retained"
        emergency_diagnostic(f"records after id {gap_after} for {self._program_name!r} are gone from history; {resume}")
    if not self._send_entries(entries):
      if reconnect:  # not the final pass: after ``close``, an emergency writer it started would never be stopped
        self.record_failure()
        self.maybe_enter()
      return True
    if entries:
      self.record_success()
      # Not the handshake's replay: a server that acks and then resets takes the replay into its buffer before the reset
      # lands, so counting it would restart the streak on every connect.
      self._drop_period = None
    return False

  @override
  def makePickle(self, record: TaggedLogRecord) -> bytes:  # pyright: ignore[reportIncompatibleMethodOverride]
    s = orjson.dumps(record_to_payload(record), default=str)
    slen = LENGTH_STRUCT.pack(len(s))
    return slen + s

  @override
  def close(self) -> None:
    """Flush history, stop the sender, then tear down the checkpoint, emergency writer, and socket.

    Reachable both from ``logging.shutdown``'s ``atexit`` pass and from the shutdown registry, so it is
    idempotent and bounded. History is flushed before the sender's last pass, so durability never waits
    on the network; the sender gets ``_LOOP_TEARDOWN_TIMEOUT`` for that pass and is abandoned (it's a
    daemon) if a stalled server holds it. Safe from any thread: it never joins the calling thread and
    never takes ``_sock_lock``, which the sender may hold while blocked in ``trigger_shutdown``, whose
    teardown runs this on another thread.

    Each base's teardown is called by name rather than through ``self``/``super()``:
    :class:`~aeth_ext.central_log_server.client.durability.RecordDurability`,
    :class:`~aeth_ext.central_log_server.client.emergency.EmergencyModeTracker`, and
    :class:`~logging.handlers.SocketHandler` all define ``close`` independently and
    none of them cooperate through the MRO, so bare ``super().close()`` would only
    ever reach one of the three.
    """
    RecordDurability.flush(self)
    self._stop.set()
    self._doorbell.put(None)
    if threading.current_thread() is not self._sender:
      self._sender.join(timeout=_LOOP_TEARDOWN_TIMEOUT)
    RecordDurability.close(self)
    EmergencyModeTracker.close(self)
    if (sock := self.sock) is not None:
      _drop_socket(sock)  # a sender abandoned mid-send still holds it; ``SocketHandler.close`` would only close it
    SocketHandler.close(self)


class AsyncioQueueDrainer(RecordDurability, EmergencyModeTracker):
  """Drain an :class:`asyncio.Queue` of :class:`~logging.LogRecord` objects to a central log server over a persistent TCP connection.

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
    """Fetch *target*'s socket-mode logging configs and set up durability state; no connection is opened here."""
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

    RecordDurability.__init__(self, self._program_name, max_history_records, max_history_bytes, max_history_age)
    EmergencyModeTracker.__init__(
      self,
      self.history_dir,
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

  def _arm_shutdown(self, trails: tuple[ExceptionTrail, ...]) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self.arm_shutdown()

  def _finish_shutdown(self, trails: tuple[ExceptionTrail, ...]) -> None:
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
    RecordDurability.flush(self)

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
        self.record_failure()
        self.maybe_enter()
        await self._sleep_or_drain(self._reconnect_delay)
        continue

      writer.write(encode_json_packet(ClientHandshake(self._program_name, self._config)))
      try:
        await writer.drain()
      except OSError:
        writer.close()
        self.record_failure()
        self.maybe_enter()
        await self._sleep_or_drain(self._reconnect_delay)
        continue

      ack = await read_server_message_async(reader)
      if isinstance(ack, HandshakeAck):
        if not ack.ok:
          reason = ack.error or "rejected without a reason"
          logger.critical("Central log server rejected the logging config for %r: %s", self._program_name, reason)
          trigger_shutdown(
            f"Central log server rejected logging config for {self._program_name!r}",
            f"Central log server rejected the logging config for {self._program_name!r}: {reason}",
          )
          writer.close()
          return
      else:
        logger.warning("Failed to read handshake ack for %r; resume-by-id will be skipped for this reconnect", self._program_name)
        alert(
          f"Failed to read handshake ack for {self._program_name!r}",
          f"Failed to read handshake ack for {self._program_name!r}: {_ACK_READ_FAILURE_REASON}",
          in_except_block=False,
        )
        ack = None

      if not await self._replay_backlog(ack, writer):
        with suppress(Exception):
          writer.close()
          await writer.wait_closed()
        self.record_failure()
        self.maybe_enter()
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
      logger.critical("Central log server rejected the logging config for %r: %s", self._program_name, message.error)
      trigger_shutdown(
        f"Central log server rejected logging config for {self._program_name!r}",
        f"Central log server rejected the logging config for {self._program_name!r}: {message.error}",
      )

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
      self.record(record, local_root=self._local_root, emergency_writer=self.writer)  # type: ignore[arg-type]
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
      self.record_success()

  async def _replay_backlog(self, ack: HandshakeAck | None, writer: asyncio.StreamWriter) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    for entry in self.resolve_backlog(ack):
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      writer.write(LENGTH_STRUCT.pack(len(payload)) + payload)
      try:
        await writer.drain()
      except OSError:
        return False
      self.mark_sent(entry.id)
    return True

  async def _sleep_or_drain(self, duration: float) -> None:
    """Wait *duration* seconds; in emergency mode drain the queue to the history file instead."""
    if self.writer is None:
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
      self.record(record, local_root=self._local_root, emergency_writer=self.writer)  # type: ignore[arg-type]
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]

  async def aclose(self) -> None:
    """Signal the drainer to stop and await its background task if one was started.

    Idempotent -- reachable both from ``__aexit__`` and from the shutdown
    registry via :meth:`_finish_shutdown`.
    """
    RecordDurability.flush(self)
    self._stop_event.set()
    if self._task is not None:
      with suppress(asyncio.CancelledError):
        await self._task
      self._task = None
    RecordDurability.close(self)
    EmergencyModeTracker.close(self)
    if self._local_manager is not None and self._local_root is not None:
      # First party imports
      from aeth_ext.central_log_server.server.dispatch import shutdown_hierarchy

      shutdown_hierarchy(self._local_manager, self._local_root)
      self._local_manager = None
      self._local_root = None

  @override
  def close(self) -> None:
    """Refuse synchronous teardown; use :meth:`aclose`.

    The inherited ``RecordDurability.close`` only closes part of this drainer's state, and the real
    teardown (:meth:`aclose`) is a coroutine that a synchronous method cannot safely await.

    Raises:
        RuntimeError: always. Call ``await drainer.aclose()`` instead.
    """
    raise RuntimeError("AsyncioQueueDrainer.close() is not supported; use 'await drainer.aclose()' instead.")

  async def __aenter__(self) -> Self:
    """Start :meth:`run` as a background task, registering for shutdown now that there is something to tear down."""
    # Registered here rather than __init__: a drainer with no _task yet has
    # nothing a shutdown needs to arm or tear down, so registering earlier
    # would just be a pointless callback for the shutdown sequence to spend
    # its budget on.
    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )
    self._stop_event.clear()
    self._task = asyncio.get_running_loop().create_task(self.run())
    return self

  async def __aexit__(self, *_: object) -> None:
    """Wait for the queue to be fully consumed, then :meth:`aclose`."""
    if isinstance(self._queue, asyncio.Queue):
      await self._queue.join()
    elif hasattr(self._queue, "join"):
      await asyncio.get_running_loop().run_in_executor(None, self._queue.join)  # type: ignore[attr-defined]
    await self.aclose()


class ThreadedQueueDrainer(RecordDurability, EmergencyModeTracker):
  """Drain a :class:`queue.Queue` of :class:`~logging.LogRecord` objects to a central log server over a persistent TCP connection.

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
    """Fetch *target*'s socket-mode logging configs and set up durability state; the drain thread is created but not started."""
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

    RecordDurability.__init__(self, self._program_name, max_history_records, max_history_bytes, max_history_age)
    EmergencyModeTracker.__init__(
      self,
      self.history_dir,
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

    self._stop_event = threading.Event()
    self._thread = threading.Thread(
      target=self._run,
      name=f"queue-drainer-{self._program_name}",
      daemon=False,
    )

  def _arm_shutdown(self, trails: tuple[ExceptionTrail, ...]) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self.arm_shutdown()

  def _finish_shutdown(self, trails: tuple[ExceptionTrail, ...]) -> None:
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
    RecordDurability.flush(self)
    self.stop(timeout=_LOOP_TEARDOWN_TIMEOUT)

  def start(self) -> None:
    """Start the background drain thread.  Must be called at most once."""
    # Registered here rather than __init__: an unstarted drainer has nothing a
    # shutdown needs to arm or tear down, so registering earlier would just be
    # a pointless callback for the shutdown sequence to spend its budget on.
    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )
    self._thread.start()

  def stop(self, timeout: float | None = None) -> None:
    """Signal the drain thread to stop and block until it exits.

    ``timeout`` is forwarded to :meth:`threading.Thread.join`; pass ``None``
    (the default) to wait indefinitely.

    Idempotent -- reachable both from ``__exit__`` and from the shutdown
    registry via :meth:`_finish_shutdown`.
    """
    RecordDurability.flush(self)
    self._stop_event.set()
    self._thread.join(timeout=timeout)
    RecordDurability.close(self)
    EmergencyModeTracker.close(self)
    if self._local_manager is not None and self._local_root is not None:
      # First party imports
      from aeth_ext.central_log_server.server.dispatch import shutdown_hierarchy

      shutdown_hierarchy(self._local_manager, self._local_root)
      self._local_manager = None
      self._local_root = None

  @override
  def close(self) -> None:
    """Alias for :meth:`stop`.

    Overridden so this drainer's teardown isn't silently shadowed by the inherited
    ``RecordDurability.close``.
    """
    self.stop()

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
    ack = read_server_message_sync(sock)
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
        self.record_failure()
        self.maybe_enter()
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

    ack = read_server_message_sync(sock)
    if isinstance(ack, HandshakeAck):
      if not ack.ok:
        reason = ack.error or "rejected without a reason"
        logger.critical("Central log server rejected the logging config for %r: %s", self._program_name, reason)
        trigger_shutdown(
          f"Central log server rejected logging config for {self._program_name!r}",
          f"Central log server rejected the logging config for {self._program_name!r}: {reason}",
        )
        with suppress(OSError):
          sock.close()
        self._stop_event.set()
        return None
    else:
      logger.warning("Failed to read handshake ack for %r; resume-by-id will be skipped for this reconnect", self._program_name)
      alert(
        f"Failed to read handshake ack for {self._program_name!r}",
        f"Failed to read handshake ack for {self._program_name!r}: {_ACK_READ_FAILURE_REASON}",
        in_except_block=False,
      )
      ack = None

    if not self._replay_backlog(ack, sock):
      with suppress(OSError):
        sock.close()
      return None

    return sock

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
    message = read_server_message_sync(sock, timeout=0.5)
    if isinstance(message, ApplyFailure):
      logger.critical("Central log server rejected the logging config for %r: %s", self._program_name, message.error)
      trigger_shutdown(
        f"Central log server rejected logging config for {self._program_name!r}",
        f"Central log server rejected the logging config for {self._program_name!r}: {message.error}",
      )
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
      self.record(record, local_root=self._local_root, emergency_writer=self.writer)  # type: ignore[arg-type]
      payload = orjson.dumps(record_to_payload(record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        if hasattr(self._queue, "task_done"):
          self._queue.task_done()  # type: ignore[attr-defined]
        return
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
      self.record_success()

  def _replay_backlog(self, ack: HandshakeAck | None, sock: socket.socket) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    for entry in self.resolve_backlog(ack):
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        return False
      self.mark_sent(entry.id)
    return True

  def _sleep_or_drain(self, duration: float) -> None:
    """Wait *duration* seconds; in emergency mode drain the queue to the history file instead."""
    if self.writer is None:
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
      self.record(record, local_root=self._local_root, emergency_writer=self.writer)  # type: ignore[arg-type]
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]

  def __enter__(self) -> Self:
    """Start the drain thread."""
    self.start()
    return self

  def __exit__(self, *_: object) -> None:
    """Wait for the queue to be fully consumed (when it supports ``join``), then :meth:`stop`."""
    if hasattr(self._queue, "join"):
      self._queue.join()  # type: ignore[attr-defined]
    self.stop()
