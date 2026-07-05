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

  def close(self) -> None:
    """Stop any background work, flushing the most recent id first."""
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
    self._thread = threading.Thread(target=self._run, name="id-checkpoint", daemon=True)
    self._thread.start()

  def schedule_persist(self, last_id: int) -> None:
    self._queue.put(last_id)

  # TODO needs a detector for FATAL_EVENT being set so it can drain the queue and exit promptly

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

  def schedule_persist(self, last_id: int) -> None:
    run_coroutine_threadsafe(self._persist(last_id), self._loop)

  # TODO needs a detector for FATAL_EVENT being set so it can drain the queue and exit promptly

  @handle_fatal_exc_async
  async def _persist(self, last_id: int) -> None:
    await to_thread(self._write, last_id)

  def close(self) -> None:
    # Fire-and-forget tasks scheduled via run_coroutine_threadsafe complete on
    # their own; there is no dedicated thread of ours to join here.
    pass
