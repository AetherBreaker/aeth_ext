# Standard library imports
import asyncio
import logging
import threading
from datetime import date, datetime
from typing import TYPE_CHECKING, Final, NamedTuple, override

# Third party imports
import orjson
from aiologic import Queue, QueueEmpty

# First party imports
# Local imports
from aeth_ext.central_log_server.server.dispatch import (
  RegisterClient,
  UnregisterClient,
  WriterItem,
  build_hierarchy,
  shutdown_hierarchy,
)
from aeth_ext.errors import FATAL_EVENT, handle_fatal_exc_async
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from pathlib import Path
  from typing import Any

  # First party imports
  from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
  from aeth_ext.logging.bases import TaggedLogRecord

logger = logging.getLogger(__name__)

settings = BaseSettings.get_settings()
_SHARED_LOG_DIR: Path = settings.persisted_dir_loc / "central_log_server"
_MIDNIGHT_BASELINE_PATH: Path = _SHARED_LOG_DIR / "midnight_baseline.json"

# Sentinel connection id for the server's own pseudo-client hierarchy, which is
# never registered or unregistered over a socket connection.
_SERVER_CONNECTION_ID: Final[int] = -1


class _Hierarchy(NamedTuple):
  """A registered private logging hierarchy plus the connection that owns it."""

  manager: logging.Manager
  root: logging.Logger
  connection_id: int


class LogWriterThread(threading.Thread):
  """Single consumer that owns every private logging hierarchy and performs all logging IO.

  The asyncio main thread only ever *produces*: it decodes each socket message
  into a :class:`~aeth_ext.logging.bases.TaggedLogRecord` and pushes it
  onto the shared queue, and it enqueues a
  :class:`~aeth_ext.central_log_server.server.dispatch.RegisterClient` /
  :class:`~aeth_ext.central_log_server.server.dispatch.UnregisterClient` event when a
  connection opens or closes. The server's own logging is routed onto the same
  queue by the root ``QueueForwardHandler`` and dispatched into a dedicated
  pseudo-client hierarchy built from *server_config*.

  This thread is the sole *consumer*. Because it is the only code that ever
  touches the registered hierarchies, registration and teardown need **no lock
  at all**. Draining a single FIFO queue also makes teardown naturally ordered:
  an ``UnregisterClient`` event sits behind every record a program already
  enqueued, so those records are flushed before its hierarchy is closed and
  none are dropped.

  This thread hosts its own asyncio event loop so it can interleave two
  concerns: dispatching records (handed off to a worker thread via
  ``asyncio.to_thread`` so file IO never blocks the loop) and
  opportunistically persisting
  :class:`~aeth_ext.central_log_server.id_registry.ClientIdRegistry` to disk
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
    id_registry: ClientIdRegistry,
    *,
    server_config: Mapping[str, Any] | None = None,
    name: str = "log-writer",
  ) -> None:
    """See the class docstring.

    Args:
        queue: The shared writer queue this thread is the sole consumer of.
        id_registry: Shared resume-state registry advanced as records land.
        server_config: Optional dict-based logging config applied into the
            server's own pseudo-client hierarchy; the server's records
            (those without a ``source_name``) are dispatched into it. When
            omitted, the server's own records are dropped with a warning.
        name: Thread name.
    """
    super().__init__(name=name)
    self._queue = queue
    self._id_registry = id_registry
    # source_name -> that program's private logging hierarchy. The None key
    # holds the server's own pseudo-client hierarchy.
    self._hierarchies: dict[str | None, _Hierarchy] = {}
    if server_config is not None:
      manager, root = build_hierarchy(server_config, settings.log_loc_folder)
      self._hierarchies[None] = _Hierarchy(manager, root, _SERVER_CONNECTION_ID)
    # Sources whose records arrived without a registered hierarchy, so the
    # warning is logged once per source rather than once per record.
    self._warned_unknown_sources: set[str | None] = set()
    # Counts updates that have landed since the last save, so a sustained,
    # never-idle burst still gets flushed periodically via MAX_UPDATES_SINCE_SAVE.
    self._updates_since_save = 0
    # Connected-program tracking is derived from the registered hierarchies
    # (see ``_update_snapshot``); no separate bookkeeping set is needed.
    # Per-program last record IDs seen by this thread (for midnight baseline).
    self._program_last_ids: dict[str, int] = {}
    # Snapshot of IDs at the start of the current day.  Seeded from the
    # persisted file so the state server reports accurate IDs-since-midnight
    # even immediately after a process restart, rather than counting only from
    # the restart time.
    self._midnight_baseline, self._snapshot_date = self._load_midnight_baseline()
    # Live read-only snapshot served by StateQueryServer.  The reference is
    # replaced atomically (CPython STORE_ATTR is GIL-protected) whenever state
    # changes, so the state server can safely read it from the main thread
    # without any additional locking.
    self._live_snapshot: dict[str, object] = {
      "connected_programs": [],
      "current_ids": {},
      "midnight_ids": {},
      "midnight_date": "",
    }

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
      # Flush and close every hierarchy (clients still connected at shutdown
      # plus the server's own) so delay-opened files are written out.
      for entry in self._hierarchies.values():
        shutdown_hierarchy(entry.manager, entry.root)
      self._hierarchies.clear()

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
    """Route a queue item to hierarchy registration, teardown, or dispatch."""
    match item:
      case RegisterClient():
        self._register_client(item)
      case UnregisterClient():
        self._unregister_client(item)
      case _:
        await self._dispatch(item)

  def _register_client(self, event: RegisterClient) -> None:
    """Adopt a connecting program's freshly built private hierarchy.

    The hierarchy arrives from the handshake already fully configured (see
    :func:`~aeth_ext.central_log_server.server.dispatch.build_hierarchy`). If a
    hierarchy from an earlier connection is still registered (its
    ``UnregisterClient`` may be racing behind a quick reconnect), it is closed
    and replaced so repeated reconnections cannot leak handlers.
    """
    stale = self._hierarchies.get(event.program_name)
    if stale is not None:
      shutdown_hierarchy(stale.manager, stale.root)
    self._hierarchies[event.program_name] = _Hierarchy(event.manager, event.root, event.connection_id)
    self._warned_unknown_sources.discard(event.program_name)
    self._update_snapshot()

  def _unregister_client(self, event: UnregisterClient) -> None:
    """Close a program's hierarchy once its connection has ended.

    Closing flushes and releases the underlying file resources. Ignored when
    the registered hierarchy belongs to a *different* connection - i.e. the
    client already reconnected and this event is the stale tail of the old
    connection.
    """
    entry = self._hierarchies.get(event.program_name)
    if entry is None or entry.connection_id != event.connection_id:
      return
    del self._hierarchies[event.program_name]
    shutdown_hierarchy(entry.manager, entry.root)
    self._update_snapshot()

  async def _dispatch(self, record: TaggedLogRecord) -> None:
    """Hand a single record to its source's private hierarchy, isolating failures.

    The record is handled by the hierarchy logger matching its ``name``, so the
    client's remote config participates in ordinary logging level/filter/
    propagation semantics. Handler-level errors are already swallowed by
    ``logging`` via ``Handler.handleError``; this guard is a last resort so an
    unexpected failure (e.g. a misbehaving filter) cannot terminate the writer
    thread and silence all subsequent logging. Advancing the id registry
    happens first so a record that a handler later fails to write is still
    accounted for from the resume protocol's point of view: it was actually
    delivered to the server.
    """
    source_name = getattr(record, "source_name", None)
    entry = self._hierarchies.get(source_name)
    if entry is None:
      if source_name not in self._warned_unknown_sources:
        self._warned_unknown_sources.add(source_name)
        logger.warning("Dropping record(s) from %r: no logging hierarchy is registered for it", source_name)
      return
    await self._update_id_registry(record)
    try:
      target = entry.root if record.name == "root" else entry.manager.getLogger(record.name)
      await asyncio.to_thread(target.handle, record)
    except Exception:
      logger.exception("Failed to dispatch log record %r", source_name)

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
    today = datetime.now(settings.tz).date()
    if self._snapshot_date is None or today != self._snapshot_date:
      self._midnight_baseline = dict(self._program_last_ids)
      self._snapshot_date = today
      self._write_midnight_baseline(today)
    updated = await self._id_registry.update(source_name, record_id, record.created)
    if not updated:
      return
    self._program_last_ids[source_name] = record_id
    self._update_snapshot()
    self._updates_since_save += 1
    if self._updates_since_save >= self.MAX_UPDATES_SINCE_SAVE:
      await self._id_registry.save()
      self._updates_since_save = 0

  def _write_midnight_baseline(self, today: date) -> None:
    """Atomically persist per-program record-ID baselines at the start of *today*."""
    try:
      _SHARED_LOG_DIR.mkdir(parents=True, exist_ok=True)
      payload: dict[str, object] = {"date": today.isoformat(), **self._midnight_baseline}
      tmp = _MIDNIGHT_BASELINE_PATH.with_name(_MIDNIGHT_BASELINE_PATH.name + ".tmp")
      tmp.write_bytes(orjson.dumps(payload))
      tmp.replace(_MIDNIGHT_BASELINE_PATH)
    except OSError:
      logger.warning("Failed to write midnight_baseline.json", exc_info=True)

  @staticmethod
  def _load_midnight_baseline() -> tuple[dict[str, int], date | None]:
    """Read the persisted midnight baseline from disk at startup.

    Returns the baseline dict and the date it was recorded on, or empty
    defaults if the file is absent, stale (from a previous day), or corrupt.
    Only baselines recorded for today are used; a file from a previous day
    cannot serve as today's midnight baseline.
    """
    today = datetime.now(settings.tz).date()
    try:
      raw: dict[str, object] = orjson.loads(_MIDNIGHT_BASELINE_PATH.read_bytes())
      if raw.get("date") != today.isoformat():
        return {}, None
      return {k: int(v) for k, v in raw.items() if k != "date"}, today  # type: ignore[arg-type]
    except OSError, ValueError, TypeError, KeyError:
      return {}, None

  def state_snapshot(self) -> dict[str, object]:
    """Return the live state snapshot, safe to call from any thread.

    The returned dict is the currently published snapshot object.  Because the
    reference is replaced atomically by the writer thread (never mutated in
    place) the caller obtains a stable, consistent snapshot it can safely
    serialise even if the writer thread publishes a newer one concurrently.
    """
    return self._live_snapshot

  def _update_snapshot(self) -> None:
    """Atomically publish a new snapshot after any state mutation.

    Constructs a fresh dict from the current state fields, then replaces
    ``_live_snapshot`` via a single attribute assignment.  In CPython a
    ``STORE_ATTR`` bytecode is executed under the GIL, making the reference
    swap atomic; ``state_snapshot()`` on any other thread therefore always
    sees a complete, consistent snapshot.
    """
    self._live_snapshot = {
      "connected_programs": sorted(name for name in self._hierarchies if name is not None),
      "current_ids": dict(self._program_last_ids),
      "midnight_ids": dict(self._midnight_baseline),
      "midnight_date": self._snapshot_date.isoformat() if self._snapshot_date else "",
    }
