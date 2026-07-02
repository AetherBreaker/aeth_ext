# Standard library imports
from collections import deque
from logging import FileHandler
from logging.handlers import DEFAULT_TCP_LOGGING_PORT, SocketHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

# Third party imports
import cloudpickle

# First party imports
from aeth_ext.settings import BaseSettings
from aeth_ext.shared_log_processor.protocol import (
  ClientLoggingHandshake,
  FilterDef,
  FormatterDef,
  HandlerDef,
  encode_packet,
)

if TYPE_CHECKING:
  # Standard library imports
  import socket
  from logging import Filter, Formatter, Handler

  # First party imports
  from aeth_ext.logging.bases import NamedLogRecord


settings = BaseSettings.get_settings()


def make_formatter_def(cls: type[Formatter], /, *args: Any, **kwargs: Any) -> FormatterDef:
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


def make_filter_def(cls: type[Filter], /, *args: Any, **kwargs: Any) -> FilterDef:
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
  /,
  *args: Any,
  project_name: str,
  formatter: FormatterDef | None = None,
  filters: tuple[FilterDef, ...] | None = None,
  startup_rollover: bool | None = None,
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

    path = path.relative_to(settings.log_loc_folder)

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
    filters=filters,
    startup_rollover=startup_rollover,
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

  def __init__(
    self,
    program_name: str,
    handlers: tuple[HandlerDef, ...],
    host: str = "localhost",
    port: int = DEFAULT_TCP_LOGGING_PORT,
    *,
    logging_base_name: str | None = None,
  ) -> None:
    super().__init__(host, port)
    self._program_name = program_name
    self._handler_defs = handlers
    self._logging_base_name = logging_base_name
    self._pending: deque[NamedLogRecord] = deque()

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
    """
    sock: socket.socket | None = self.sock
    if sock is None:
      return
    handshake = ClientLoggingHandshake(
      program_name=self._program_name,
      handlers=self._handler_defs,
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

  @override
  def emit(self, record: NamedLogRecord) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Buffer *record* then flush as many buffered records as possible.

    If the underlying socket is broken, records accumulate in
    ``_pending`` and are retried (in order) on the next :meth:`emit`
    call, which may trigger a reconnect via :meth:`createSocket`.
    """
    self._pending.append(record)
    self._flush_pending()

  def _flush_pending(self) -> None:
    """Drain ``_pending`` in FIFO order, stopping at the first send failure.

    ``SocketHandler.send`` swallows :exc:`OSError` and sets ``self.sock``
    to ``None`` on failure, so a ``None`` socket after the call is the
    reliable signal that the record was *not* delivered.
    """
    while self._pending:
      record = self._pending[0]
      try:
        s = self.makePickle(record)
        self.send(s)
      except Exception:
        # makePickle or an unexpected error — discard the offending record
        # so it doesn't block the queue, then let the caller know.
        self._pending.popleft()
        self.handleError(record)
        continue
      if self.sock is None:
        # send() failed silently; leave the record at the front so the
        # next emit (which may reconnect) retries it first.
        break
      self._pending.popleft()
