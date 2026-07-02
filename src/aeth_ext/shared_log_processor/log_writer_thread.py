# Standard library imports
import logging
import threading
from typing import Final, override

# Third party imports
from aiologic import Queue, QueueEmpty

# First party imports
from aeth_ext.errors import FATAL_EVENT

# Local imports
from aeth_ext.shared_log_processor.dispatch import (
  ProgramFilter,
  RegisterHandlers,
  UnregisterHandlers,
  WriterItem,
)

logger = logging.getLogger(__name__)


class LogWriterThread(threading.Thread):
  """Single consumer that owns the dispatch logger and performs all logging IO.

  The asyncio main thread only ever *produces*: it decodes each socket message
  into a :class:`~aeth_ext.shared_log_processor.protocol.LabelledLogRecord` and pushes it
  onto the shared queue, and it enqueues a
  :class:`~aeth_ext.shared_log_processor.dispatch.RegisterHandlers` /
  :class:`~aeth_ext.shared_log_processor.dispatch.UnregisterHandlers` event when a
  connection opens or closes. The server's own logging is routed onto the same
  queue by the root ``QueueForwardHandler``.

  This thread is the sole *consumer*. Because it is the only code that ever
  touches the dispatch logger's handler list, handler registration and teardown
  need **no lock at all**. Draining a single FIFO queue also makes teardown
  naturally ordered: an ``UnregisterHandlers`` event sits behind every record a
  program already enqueued, so those records are flushed before its handlers are
  closed and none are dropped.

  Moving the ``handle`` call (and therefore all handler/file IO) onto this
  dedicated thread keeps the event loop from blocking on disk writes, so the
  main thread stays responsive to sockets while this thread absorbs the IO wait.

  Shutdown is driven by :data:`aeth_ext.errors.FATAL_EVENT` - the same event the
  main coroutine watches - so a single signal tears the whole process down.
  Because the queue is polled with a short timeout rather than blocked on
  indefinitely, the thread notices the event promptly and then drains whatever
  is still queued so nothing is lost on the way out.
  """

  # How long each blocking ``green_get`` waits before rechecking FATAL_EVENT.
  POLL_INTERVAL: Final[float] = 0.5

  def __init__(
    self,
    queue: Queue[WriterItem],
    dispatch_logger: logging.Logger,
    *,
    name: str = "log-writer",
  ) -> None:
    super().__init__(name=name)
    self._queue = queue
    self._dispatch_logger = dispatch_logger
    # program_name -> the handlers this thread registered for it, for teardown.
    self._program_handlers: dict[str, list[logging.Handler]] = {}

  @override
  def run(self) -> None:
    while not FATAL_EVENT.is_set():
      try:
        item = self._queue.green_get(timeout=self.POLL_INTERVAL)
      except QueueEmpty:
        continue

      self._process(item)

    self._drain()

  def _drain(self) -> None:
    """Process everything still queued at shutdown so nothing is dropped.

    Keeps going until the queue is empty *and* no producer is still blocked
    trying to hand an item over (see :attr:`Queue.putting`), so an item caught
    mid-``put`` is waited out rather than lost.
    """
    while True:
      try:
        item = self._queue.green_get(blocking=False)
      except QueueEmpty:
        if not self._queue.putting:
          break
        # A producer is mid-put; block briefly to let the handoff complete.
        try:
          item = self._queue.green_get(timeout=self.POLL_INTERVAL)
        except QueueEmpty:
          continue

      self._process(item)

  def _process(self, item: WriterItem) -> None:
    """Route a queue item to handler registration, teardown, or dispatch."""
    match item:
      case RegisterHandlers():
        self._register_handlers(item)
      case UnregisterHandlers():
        self._unregister_handlers(item)
      case _:
        self._dispatch(item)

  def _register_handlers(self, event: RegisterHandlers) -> None:
    """Assign a connecting program's handlers to the shared dispatch logger.

    Each handler arrives from the handshake already built with its formatter and
    any client-supplied filters. Because the single dispatch logger holds every
    program's handlers at once, each is additionally stamped with a
    :class:`ProgramFilter` so ordinary logging filtering only lets records
    carrying this program's ``source_name`` reach it.
    """
    handshake = event.handshake
    program_filter = ProgramFilter(handshake.program_name)
    registered = self._program_handlers.setdefault(handshake.program_name, [])
    for handler in handshake.handlers:
      handler.addFilter(program_filter)
      self._dispatch_logger.addHandler(handler)
      registered.append(handler)

  def _unregister_handlers(self, event: UnregisterHandlers) -> None:
    """Detach and close a program's handlers once its connection has ended.

    Removing them from the dispatch logger stops further records from routing to
    this program's files, and closing flushes and releases the underlying file
    resources so repeated reconnections cannot leak handlers.
    """
    for handler in self._program_handlers.pop(event.program_name, ()):
      self._dispatch_logger.removeHandler(handler)
      handler.close()

  def _dispatch(self, record: logging.LogRecord) -> None:
    """Hand a single record to the dispatch logger, isolating failures.

    Handler-level errors are already swallowed by ``logging`` via
    ``Handler.handleError``; this guard is a last resort so an unexpected
    failure (e.g. a misbehaving filter) cannot terminate the writer thread and
    silence all subsequent logging.
    """
    try:
      self._dispatch_logger.handle(record)
    except Exception:
      logger.exception("Failed to dispatch log record %r", getattr(record, "source_name", None))
