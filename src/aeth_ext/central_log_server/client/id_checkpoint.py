# Standard library imports
import logging
import threading
from asyncio import run_coroutine_threadsafe, to_thread
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
  # Standard library imports
  from asyncio import AbstractEventLoop
  from pathlib import Path

# First party imports
from aeth_ext.errors import handle_fatal_exc_async, handle_fatal_exc_sync

logger = logging.getLogger(__name__)

__all__ = ["AsyncioIdCheckpointBackend", "IdCheckpointBackend", "ThreadedIdCheckpointBackend"]

_SHUTDOWN: Final = object()


class IdCheckpointBackend(Protocol):
  """Durably persists the last-assigned client record id, non-blockingly.

  ``load`` is only ever called once at handler construction and may block;
  ``schedule_persist`` is called from the (synchronous, lock-held) logging
  ``emit`` path on every record, so it must never block.
  """

  def load(self) -> int:
    """Return the last persisted id, or ``0`` if none has been persisted yet."""
    ...

  def schedule_persist(self, last_id: int) -> None:
    """Arrange for *last_id* to be durably persisted, without blocking."""
    ...

  def begin_shutdown(self) -> None:
    """Switch to writing every id through synchronously (D-I6/D-I8).

    Registered for :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT`, so an
    implementation must do nothing here but one atomic store -- no I/O, no
    locks, no logging. The behaviour change lands on the *next*
    :meth:`schedule_persist`, which by then is back on an ordinary thread.

    Trading ``schedule_persist``'s non-blocking guarantee for durability is
    deliberate and scoped to shutdown: from here on there may be no later
    opportunity to persist at all, and the write is a single small atomic
    replace.
    """
    ...

  def close(self) -> None:
    """Stop any background work, flushing the most recent id first.

    Must be idempotent -- it is reachable both from the owning handler's own
    teardown and from the shutdown registry.
    """
    ...


class _FileCheckpointMixin:
  """Shared atomic file read/write helpers for the two backends below."""

  def __init__(self, path: Path) -> None:
    self._path = path

  def load(self) -> int:
    try:
      return int(self._path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
      return 0

  def _write(self, last_id: int) -> None:
    self._path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self._path.with_suffix(f"{self._path.suffix}.tmp")
    tmp.write_text(str(last_id), encoding="utf-8")
    try:
      tmp.replace(self._path)
    except PermissionError:
      # Windows: the target may be locked by another process; delete it first, then rename.
      self._path.unlink(missing_ok=True)
      tmp.replace(self._path)


class ThreadedIdCheckpointBackend(_FileCheckpointMixin):
  """Persists the id on a dedicated daemon thread, coalescing pending writes.

  Only the most recently scheduled id is ever written - if several records
  are emitted faster than disk IO can keep up, only the latest value needs to
  survive, so intermediate values queued in between are safely dropped.
  """

  def __init__(self, path: Path) -> None:
    super().__init__(path)
    self._queue: SimpleQueue[int | object] = SimpleQueue()
    self._shutting_down = False
    self._thread = threading.Thread(target=self._run, name="id-checkpoint", daemon=True)
    self._thread.start()

  def schedule_persist(self, last_id: int) -> None:
    if self._shutting_down:
      # Write-through: the worker thread is daemonised, so anything still
      # sitting in the queue when the interpreter exits is simply abandoned.
      self._write(last_id)
      return
    self._queue.put(last_id)

  def begin_shutdown(self) -> None:
    """See :meth:`IdCheckpointBackend.begin_shutdown`. One atomic store only."""
    self._shutting_down = True

  @handle_fatal_exc_sync
  def _run(self) -> None:
    while True:
      item = self._queue.get()
      if item is _SHUTDOWN:
        return
      latest = item
      while True:
        try:
          item = self._queue.get_nowait()
        except Empty:
          break
        if item is _SHUTDOWN:
          self._write(latest)  # pyright: ignore[reportArgumentType]
          return
        latest = item
      self._write(latest)  # pyright: ignore[reportArgumentType]

  def close(self) -> None:
    self._queue.put(_SHUTDOWN)
    self._thread.join(timeout=5.0)


class AsyncioIdCheckpointBackend(_FileCheckpointMixin):
  """Persists the id via the host program's own asyncio event loop.

  For programs that are already asyncio-based and would rather avoid an
  extra standing thread, this schedules a coroutine onto the caller-supplied
  loop (from whatever thread ``schedule_persist`` happens to be called on),
  which offloads the actual file write with :func:`asyncio.to_thread`.
  """

  def __init__(self, path: Path, loop: AbstractEventLoop) -> None:
    super().__init__(path)
    self._loop = loop
    self._shutting_down = False
    # The most recent id handed to schedule_persist, whether or not its
    # coroutine ever ran. This is what makes close() able to guarantee anything
    # at all -- see there.
    self._last_scheduled: int | None = None

  def schedule_persist(self, last_id: int) -> None:
    self._last_scheduled = last_id
    if self._shutting_down:
      # Write-through, and deliberately *not* via the loop: during shutdown the
      # loop may be exactly what is wedged, and a coroutine scheduled onto it
      # would never run. A direct write costs one small atomic replace and
      # cannot be starved.
      self._write(last_id)
      return
    run_coroutine_threadsafe(self._persist(last_id), self._loop)

  def begin_shutdown(self) -> None:
    """See :meth:`IdCheckpointBackend.begin_shutdown`. One atomic store only."""
    self._shutting_down = True

  @handle_fatal_exc_async
  async def _persist(self, last_id: int) -> None:
    await to_thread(self._write, last_id)

  def close(self) -> None:
    """Persist the most recently scheduled id synchronously.

    :meth:`schedule_persist` drops the future from
    ``run_coroutine_threadsafe`` on the floor, so a coroutine that has not run
    yet when the loop stops takes its id with it. This previously did nothing
    at all, on the reasoning that fire-and-forget tasks "complete on their
    own" -- which holds only while the loop is alive, i.e. never during the
    shutdown that matters.

    Writing directly rather than waiting on those futures is intentional: it
    does not depend on the loop making progress, and re-writing an id that a
    coroutine already persisted is harmless because the write is an atomic
    replace of the same value.

    Idempotent, as the Protocol requires.
    """
    if self._last_scheduled is None:
      return
    try:
      self._write(self._last_scheduled)
    except OSError:
      logger.exception("Failed to persist the final id checkpoint to %s", self._path)
