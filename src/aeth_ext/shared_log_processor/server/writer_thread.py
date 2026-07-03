# Standard library imports
import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Final, override

# Third party imports
from aiologic import Queue, QueueEmpty

# First party imports
from aeth_ext.errors import FATAL_EVENT, handle_fatal_exc_async

# Local imports
from aeth_ext.shared_log_processor.server.dispatch import (
  ProgramFilter,
  RegisterHandlers,
  UnregisterHandlers,
  WriterItem,
)

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.shared_log_processor.protocol import TaggedLogRecord
  from aeth_ext.shared_log_processor.server.id_registry import ClientIdRegistry

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

  This thread hosts its own asyncio event loop so it can interleave two
  concerns: dispatching records (handed off to a worker thread via
  ``asyncio.to_thread`` so file IO never blocks the loop) and
  opportunistically persisting
  :class:`~aeth_ext.shared_log_processor.id_registry.ClientIdRegistry` to disk
  whenever the queue drains empty, so the last-id-per-program mapping
  survives an abrupt crash rather than only a graceful shutdown, without
  paying for a disk write on every single record. Under sustained load where
  the queue never idles, :attr:`MAX_UPDATES_SINCE_SAVE` forces a save every so
  many updates anyway, so a crash mid-burst can't lose an unbounded amount of
  resume state.

  Shutdown is driven by :data:`aeth_ext.errors.FATAL_EVENT` - the same event the
  main coroutine watches - so a single signal tears the whole process down.
  Because the queue is polled with a short timeout rather than awaited
  indefinitely, the thread notices the event promptly and then drains whatever
  is still queued so nothing is lost on the way out, followed by one final
  synchronous save of the id registry.
  """

  # How long each ``async_get`` waits before rechecking FATAL_EVENT. Also the
  # cadence at which an idle queue triggers an opportunistic registry save.
  POLL_INTERVAL: Final[float] = 0.5

  # Fallback for sustained load where the queue never idles long enough to
  # trigger the opportunistic save above: force a save after this many
  # updates land, bounding how much resume state a crash could lose.
  MAX_UPDATES_SINCE_SAVE: Final[int] = 100

  def __init__(
    self,
    queue: Queue[WriterItem],
    dispatch_logger: logging.Logger,
    id_registry: ClientIdRegistry,
    *,
    name: str = "log-writer",
  ) -> None:
    super().__init__(name=name)
    self._queue = queue
    self._dispatch_logger = dispatch_logger
    self._id_registry = id_registry
    # program_name -> the handlers this thread registered for it, for teardown.
    self._program_handlers: dict[str, list[logging.Handler]] = {}
    # Counts updates that have landed since the last save, so a sustained,
    # never-idle burst still gets flushed periodically via MAX_UPDATES_SINCE_SAVE.
    self._updates_since_save = 0

  @override
  def run(self) -> None:
    # asyncio.run() ignores set_event_loop() and always creates a fresh loop
    # via the default policy, so the optimized loop from initialize() would
    # never be used here if we called asyncio.run() bare.  Pass loop_factory
    # explicitly so this thread also gets winloop/uvloop when available.
    # Standard library imports
    from sys import platform

    try:
      if platform in ("win32", "cygwin", "cli"):
        # Third party imports
        from winloop import new_event_loop as _new_event_loop
      else:
        # Third party imports
        from uvloop import new_event_loop as _new_event_loop  # type: ignore[no-redef]
      asyncio.run(self._amain(), loop_factory=_new_event_loop)
    except ImportError:
      asyncio.run(self._amain())

  @handle_fatal_exc_async
  async def _amain(self) -> None:
    try:
      await self._record_loop()
    finally:
      # One last synchronous save on top of the opportunistic ones, so a
      # clean shutdown always captures whatever changed most recently.
      await self._id_registry.save()

  async def _record_loop(self) -> None:
    while not FATAL_EVENT.is_set():
      try:
        item = await asyncio.wait_for(self._queue.async_get(), timeout=self.POLL_INTERVAL)
      except QueueEmpty, TimeoutError:
        # Nothing arrived within POLL_INTERVAL, i.e. the queue is drained and
        # idle - a good opportunity to persist state cheaply, since save()
        # is a no-op unless something actually changed since the last call.
        await self._id_registry.save()
        self._updates_since_save = 0
        continue

      await self._process(item)

    await self._drain()

  async def _drain(self) -> None:
    """Process everything still queued at shutdown so nothing is dropped.

    Keeps going until the queue is empty *and* no producer is still blocked
    trying to hand an item over (see :attr:`Queue.putting`), so an item caught
    mid-``put`` is waited out rather than lost.
    """
    while True:
      try:
        item = await self._queue.async_get(blocking=False)
      except QueueEmpty:
        if not self._queue.putting:
          break
        # A producer is mid-put; wait briefly to let the handoff complete.
        try:
          item = await asyncio.wait_for(self._queue.async_get(), timeout=self.POLL_INTERVAL)
        except QueueEmpty, TimeoutError:
          continue

      await self._process(item)

  async def _process(self, item: WriterItem) -> None:
    """Route a queue item to handler registration, teardown, or dispatch."""
    match item:
      case RegisterHandlers():
        self._register_handlers(item)
      case UnregisterHandlers():
        self._unregister_handlers(item)
      case _:
        await self._dispatch(item)

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

  async def _dispatch(self, record: TaggedLogRecord) -> None:
    """Hand a single record to the dispatch logger, isolating failures.

    Handler-level errors are already swallowed by ``logging`` via
    ``Handler.handleError``; this guard is a last resort so an unexpected
    failure (e.g. a misbehaving filter) cannot terminate the writer thread and
    silence all subsequent logging. Advancing the id registry happens first so
    a record that a handler later fails to write is still accounted for from
    the resume protocol's point of view: it was actually delivered to the
    server.
    """
    await self._update_id_registry(record)
    try:
      await asyncio.to_thread(self._dispatch_logger.handle, record)
    except Exception:
      logger.exception("Failed to dispatch log record %r", getattr(record, "source_name", None))

  async def _update_id_registry(self, record: TaggedLogRecord) -> None:
    """Advance the id registry if *record* carries client resume metadata.

    Records produced by the server's own logging (routed via
    ``QueueForwardHandler``) carry neither ``source_name`` nor
    ``record_id``, so those are left alone. A sustained, never-idle
    burst of records would otherwise never hit the opportunistic save in
    :meth:`_record_loop`, so this also forces a save every
    :attr:`MAX_UPDATES_SINCE_SAVE` actual updates.
    """
    source_name = record.source_name
    record_id = record.record_id
    if source_name is None or record_id is None:
      return
    updated = await self._id_registry.update(source_name, record_id, record.created)
    if not updated:
      return
    self._updates_since_save += 1
    if self._updates_since_save >= self.MAX_UPDATES_SINCE_SAVE:
      await self._id_registry.save()
      self._updates_since_save = 0
