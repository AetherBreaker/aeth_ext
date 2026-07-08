# Standard library imports
import asyncio
from dataclasses import dataclass
from datetime import datetime
from logging import getLogger
from typing import Self

# Third party imports
import orjson
from aiologic import Lock

# First party imports
from aeth_ext.settings import BaseSettings

logger = getLogger(__name__)

__all__ = [
  "ClientIdRegistry",
  "ClientIdState",
]

settings = BaseSettings.get_settings()


@dataclass(slots=True, frozen=True)
class ClientIdState:
  """The last record a connecting program is known to have delivered.

  ``last_received_at`` is the record's own ``created`` timestamp (i.e. when
  the client emitted it), not when the server processed it, so the client can
  use it to pick the right date-segregated history file on resume.
  """

  last_record_id: int
  last_received_at: datetime


class ClientIdRegistry:
  """Tracks, per connected program, the last record id the server has seen.

  Every connected program gets its own independent id sequence, but all of
  them are persisted together in a single human-readable JSON file (rather
  than one file per program) so the on-disk footprint stays flat regardless
  of how many distinct programs have ever connected.

  Reads (:meth:`get`) happen from the asyncio main thread right after a
  handshake is decoded; writes (:meth:`update`) happen from the writer
  thread's own event loop as records are dispatched. Both are coroutines
  guarded by an :class:`aiologic.Lock`, which - unlike a plain
  :class:`threading.Lock` - suspends only the calling *task* while waiting for
  the lock rather than blocking the entire OS thread (and every other task
  scheduled on its event loop), even though the two callers live on two
  different threads/loops. :meth:`save` snapshots the mapping under the lock
  and then writes it to disk via an atomic replace on a worker thread (via
  :func:`asyncio.to_thread`), so a periodic caller can persist state without
  ever risking a torn/partial file on crash or blocking its event loop on
  disk IO.
  """

  _path = settings.persisted_dir_loc / "client_ids.json"

  def __init__(self) -> None:
    self._lock = Lock()
    self._states: dict[str, ClientIdState] = {}
    self._dirty = False

  @classmethod
  def load(cls) -> Self:
    """Build a registry, seeding it from the default path if it already exists.

    Called once at startup before either the server or the writer thread
    begins handling connections/records, so no locking is needed here.
    """
    registry = cls()
    if not cls._path.exists():
      return registry

    try:
      raw: dict[str, dict[str, object]] = orjson.loads(cls._path.read_bytes())
    except OSError, ValueError:
      logger.warning("Could not read client id registry at %s; starting empty", cls._path, exc_info=True)
      return registry

    for program_name, entry in raw.items():
      try:
        registry._states[program_name] = ClientIdState(
          last_record_id=int(entry["last_record_id"]),  # pyright: ignore[reportArgumentType]
          last_received_at=datetime.fromisoformat(str(entry["last_received_at"])),
        )
      except KeyError, TypeError, ValueError:
        logger.warning("Skipping malformed client id registry entry for %r", program_name, exc_info=True)

    return registry

  async def get(self, program_name: str) -> ClientIdState | None:
    """Return the last known state for *program_name*, or ``None`` if unseen."""
    async with self._lock:
      return self._states.get(program_name)

  async def update(self, program_name: str, record_id: int, received_at: float) -> bool:
    """Advance *program_name*'s state if *record_id* is newer than what's stored.

    Records are expected to arrive in order (the client's send path is
    strictly FIFO), but this guard makes stale/duplicate updates a no-op
    rather than letting them regress the stored id.

    Returns whether the state actually advanced, so callers can track how
    many updates have accumulated since the last :meth:`save`.
    """
    async with self._lock:
      existing = self._states.get(program_name)
      if existing is not None and existing.last_record_id >= record_id:
        return False
      self._states[program_name] = ClientIdState(record_id, datetime.fromtimestamp(received_at, tz=settings.tz))
      self._dirty = True
      return True

  async def save(self) -> None:
    """Persist the current state to disk, atomically, if anything changed.

    Safe to call frequently (e.g. from a periodic task): it is a no-op unless
    :meth:`update` has recorded a change since the last successful save.
    """
    async with self._lock:
      if not self._dirty:
        return
      payload = {
        program_name: {
          "last_record_id": state.last_record_id,
          "last_received_at": state.last_received_at.isoformat(),
        }
        for program_name, state in self._states.items()
      }
      self._dirty = False

    await asyncio.to_thread(self._write_payload, payload)

  def _write_payload(self, payload: dict[str, dict[str, object]]) -> None:
    """Blocking disk write; run off the event loop via :func:`asyncio.to_thread`."""
    self._path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
    tmp_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    tmp_path.replace(self._path)
