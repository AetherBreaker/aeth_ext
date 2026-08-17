"""Two-tier (Transport, channel) bookkeeping for `SFTPAdapter`'s channel multiplexing.

`ChannelLedger` holds the shared, self-locking state; `SFTPChannelPool` makes every decision on top of
it and is `AdaptedSFTP`'s `HandleProvider`. Kept out of `adapter.py` so the plain-FTP pooling path
(unchanged fixed one-connection-per-slot queue) isn't diluted by SFTP-only concepts, and kept entirely
out of `AdaptedSFTP` -- that class only ever sees a bare `SFTPClient` handler, identical in shape to
`AdaptedFTP`'s. This module is the only place that knows a checked-out `SFTPClient` came from a
specific `Transport`.
"""

# Standard library imports
import errno
from dataclasses import dataclass
from logging import getLogger
from queue import Queue
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, Protocol

# Third party imports
from paramiko import SSHException

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence

  # Third party imports
  from paramiko import SFTPClient, Transport

  # First party imports
  from aeth_ext.ftp.types import IntrumentCallable, TransportProvider

__all__ = ["Channel", "ChannelLedger", "LockedDict", "LockedList", "SFTPChannelPool", "TransportState"]

logger = getLogger(__name__)


class _ChannelConnector(Protocol):
  """Structural shape of `_SFTPConnector` (`adapter.py`) that `SFTPChannelPool` needs -- spelled out
  locally rather than imported, since importing the concrete class would cycle back through
  `adapter.py`'s own (real, runtime) import of this module."""

  __slots__ = ()

  def request_handler(self, transport: Transport) -> SFTPClient: ...
  def close_conn_handler(self, handle: Transport) -> None: ...


@dataclass(slots=True)
class TransportState:
  """Per-`Transport` bookkeeping: how many channels it currently holds and its measured throughput."""

  transport: Transport
  channel_count: int = 0
  ewma_throughput: float | None = None
  sample_count: int = 0

  _EWMA_ALPHA: ClassVar[float] = 0.3
  _MIN_SAMPLES: ClassVar[int] = 3
  _SATURATION_RATIO: ClassVar[float] = 0.6

  def update_throughput(self, nbytes: int, elapsed: float) -> None:
    """Blends a new sample into the running EWMA throughput estimate.

    Args:
      nbytes: Bytes transferred in this sample.
      elapsed: Seconds elapsed for this sample.
    """
    rate = nbytes / max(elapsed, 1e-6)
    if self.ewma_throughput is None:
      self.ewma_throughput = rate
    else:
      self.ewma_throughput = self._EWMA_ALPHA * rate + (1 - self._EWMA_ALPHA) * self.ewma_throughput
    self.sample_count += 1

  def is_saturated(self, best_throughput: float) -> bool:
    """Reports whether this transport's throughput is meaningfully below `best_throughput`.

    Args:
      best_throughput: The best throughput currently observed across all transports.

    Returns:
      `True` if saturated; always `False` until at least `_MIN_SAMPLES` samples have been recorded.
    """
    if self.ewma_throughput is None or self.sample_count < self._MIN_SAMPLES:
      return False
    return self.ewma_throughput < best_throughput * self._SATURATION_RATIO


@dataclass(slots=True)
class Channel:
  """A checked-out or idle SFTP handle, tagged with the `TransportState` it was opened from."""

  handle: SFTPClient
  state: TransportState


class LockedDict[K, V]:
  """A dict whose mutating/reading operations are individually atomic under one shared lock.

  `values()`/`clear()` copy under the lock and return a plain list, so callers never iterate while
  holding the lock.
  """

  def __init__(self, lock: RLock) -> None:
    self._data: dict[K, V] = {}
    self._lock = lock

  def __setitem__(self, key: K, value: V) -> None:
    with self._lock:
      self._data[key] = value

  def __getitem__(self, key: K) -> V:
    with self._lock:
      return self._data[key]

  def __delitem__(self, key: K) -> None:
    with self._lock:
      del self._data[key]

  def __contains__(self, key: K) -> bool:
    with self._lock:
      return key in self._data

  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  def get(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
      return self._data.get(key, default)

  def pop(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
      return self._data.pop(key, default)

  def values(self) -> list[V]:
    with self._lock:
      return list(self._data.values())

  def clear(self) -> list[V]:
    """Empties the dict, returning everything it held (for teardown)."""
    with self._lock:
      values = list(self._data.values())
      self._data.clear()
      return values


class LockedList[T]:
  """A list whose mutating/reading operations are individually atomic under one shared lock."""

  def __init__(self, lock: RLock) -> None:
    self._data: list[T] = []
    self._lock = lock

  def append(self, item: T) -> None:
    with self._lock:
      self._data.append(item)

  def pop(self) -> T | None:
    with self._lock:
      return self._data.pop() if self._data else None

  def remove(self, item: T) -> None:
    with self._lock:
      if item in self._data:
        self._data.remove(item)

  def __contains__(self, item: T) -> bool:
    with self._lock:
      return item in self._data

  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  def clear(self) -> list[T]:
    with self._lock:
      values = list(self._data)
      self._data.clear()
      return values


class ChannelLedger:
  """Shared, self-locking books for `SFTPChannelPool`. Holds data only -- every read/write here is a
  single, obvious operation; anything that has to touch more than one of these atomically (e.g. "is this
  transport saturated, and if not, put its channel back in `idle`") is `SFTPChannelPool`'s job, done with
  an explicit `with ledger.lock:` block, not a method on this class.
  """

  def __init__(self, transports: TransportProvider) -> None:
    self.lock = RLock()
    self.transports = transports
    self.pool: SFTPChannelPool | None = None  # filled in by SFTPAdapter once the pool exists
    self.states: LockedDict[int, TransportState] = LockedDict(self.lock)
    self.handle_states: LockedDict[int, TransportState] = LockedDict(self.lock)
    self.idle: LockedList[Channel] = LockedList(self.lock)
    self.in_flight = 0
    self.wave_running_max = 0.0
    self.last_wave_best_throughput: float | None = None


class SFTPChannelPool:
  """Owns every acquire/release/growth/saturation decision on top of a `ChannelLedger`. `AdaptedSFTP`'s
  `HandleProvider`."""

  def __init__(self, ledger: ChannelLedger, connector: _ChannelConnector, channels_per_transport: int) -> None:
    """Initializes an empty pool bound to `ledger`'s state and `connector`'s connection-opening.

    Args:
      ledger: The shared bookkeeping this pool reads and writes.
      connector: Opens channels on an existing `Transport` and closes whole `Transport`s.
      channels_per_transport: Maximum channels to multiplex onto a single `Transport`.
    """
    self._ledger = ledger
    self._connector = connector
    self.channels_per_transport = channels_per_transport
    self._wakeup: Queue[None] = Queue()

  def acquire(self) -> tuple[SFTPClient, Sequence[IntrumentCallable]]:
    """Checks out an idle channel if one validates, else multiplexes a new channel onto an
    under-cap `Transport`, dials a brand new `Transport`, or (if the pool is fully saturated)
    blocks until a channel is released.

    Returns:
      The handle, plus a throughput-instrumentation observer callback for it.
    """
    channel = self._checkout_idle()
    if channel is not None and not self._validate(channel.handle):
      self._discard(channel)
      channel = None

    if channel is None:
      target = self._pick_growth_target()
      if target is None:
        transport = self._ledger.transports.open_transport()
        if transport is not None:
          target = TransportState(transport=transport)
          self._ledger.states[id(transport)] = target
      if target is not None:
        handle = self._connector.request_handler(target.transport)
        with self._ledger.lock:
          target.channel_count += 1
          self._ledger.handle_states[id(handle)] = target
          self._ledger.in_flight += 1
        channel = Channel(handle=handle, state=target)

    if channel is None:
      channel = self._checkout_blocking()

    return channel.handle, (self._make_instrument(channel.state),)

  def release(self, handle: SFTPClient, is_fatal: bool) -> None:
    """Returns a handle to its `Transport`'s pool, or discards it (and the whole `Transport` if it's
    no longer active) when `is_fatal` marks it broken.

    Args:
      handle: The handle to return or discard.
      is_fatal: Whether the connection is broken and should be discarded rather than pooled.
    """
    state = self._ledger.handle_states.get(id(handle))
    assert state is not None, "handle must have been tracked at checkout"
    channel = Channel(handle=handle, state=state)

    if not is_fatal:
      if self._release_or_pop_saturated(channel):
        self._close_quietly(channel.handle)
      return

    if state.transport.is_active():
      self._discard(channel)
      return

    orphaned = self._drop_transport(state)
    for orphan in (channel, *orphaned):
      self._close_quietly(orphan.handle)
    self._ledger.transports.transport_dropped()

  def teardown(self) -> None:
    """Closes every tracked `Transport` (and every channel opened on it)."""
    for state in self._ledger.states.clear():
      self._close_quietly_transport(state.transport)
    self._ledger.handle_states.clear()
    self._ledger.idle.clear()

  def keepalive_check_one(self) -> None:
    """Pops and validates one idle channel, discarding it (and possibly its `Transport`) if the
    check fails."""
    channel = self._checkout_idle()
    if channel is None:
      return
    self.release(channel.handle, is_fatal=not self._validate(channel.handle))

  def _checkout_idle(self) -> Channel | None:
    """Pops an idle channel for reuse, or returns `None` if none is idle."""
    channel = self._ledger.idle.pop()
    if channel is None:
      return None
    with self._ledger.lock:
      self._ledger.in_flight += 1
    return channel

  def _checkout_blocking(self) -> Channel:
    """Blocks until an idle channel becomes available, then checks it out."""
    while True:
      channel = self._checkout_idle()
      if channel is not None:
        return channel
      self._wakeup.get()

  def _best_live_throughput(self) -> float | None:
    """Returns the best currently-tracked throughput, falling back to the last completed wave's best
    if no transport has a live sample yet."""
    samples = [s.ewma_throughput for s in self._ledger.states.values() if s.ewma_throughput is not None]
    if samples:
      return max(samples)
    return self._ledger.last_wave_best_throughput

  def _pick_growth_target(self) -> TransportState | None:
    """Returns a `TransportState` under its channel cap to open a new channel on, or `None` if
    every live `Transport` is at cap or saturated (the caller should dial a new `Transport` instead).
    """
    best = self._best_live_throughput()
    candidates = [
      s
      for s in self._ledger.states.values()
      if s.channel_count < self.channels_per_transport and not (best is not None and s.is_saturated(best))
    ]
    if not candidates:
      return None
    return min(candidates, key=lambda s: s.channel_count)

  def _release_or_pop_saturated(self, channel: Channel) -> bool:
    """Returns a checked-out channel to the pool, or pops it if its `Transport` is saturated.

    Args:
      channel: The channel being released.

    Returns:
      `True` if the channel was popped (its `Transport` is saturated -- caller must close the
      handle; `channel_count` has already been decremented here), `False` if it was returned to
      `ledger.idle` for reuse.
    """
    best = self._best_live_throughput()
    with self._ledger.lock:
      if best is not None and channel.state.is_saturated(best):
        channel.state.channel_count -= 1
        self._ledger.handle_states.pop(id(channel.handle), None)
        popped = True
      else:
        self._ledger.idle.append(channel)
        popped = False
    self._wakeup.put_nowait(None)  # a slot freed up either way -- saturated pop or a fresh idle channel
    self._mark_returned(channel.state)
    return popped

  def _mark_returned(self, state: TransportState) -> None:
    """Records a channel as no longer in-flight, folding its throughput into the current wave's max.

    Once every in-flight channel has returned, snapshots the wave's max into
    `ledger.last_wave_best_throughput` and resets for the next wave.

    Args:
      state: The `TransportState` of the channel that just returned.
    """
    with self._ledger.lock:
      if state.ewma_throughput is not None:
        self._ledger.wave_running_max = max(self._ledger.wave_running_max, state.ewma_throughput)
      self._ledger.in_flight -= 1
      if self._ledger.in_flight == 0:
        self._ledger.last_wave_best_throughput = self._ledger.wave_running_max
        self._ledger.wave_running_max = 0.0

  def _discard(self, channel: Channel) -> None:
    """Stops tracking a channel (removing it from `ledger.idle` if present) and closes its handle.

    Args:
      channel: The channel to discard.
    """
    with self._ledger.lock:
      channel.state.channel_count -= 1
      self._ledger.handle_states.pop(id(channel.handle), None)
      self._ledger.idle.remove(channel)
    self._mark_returned(channel.state)
    self._close_quietly(channel.handle)

  def _drop_transport(self, state: TransportState) -> list[Channel]:
    """Stops tracking a `Transport`, returning whichever of its channels were sitting idle.

    Channels still checked out elsewhere are not returned here -- they aren't tracked in `ledger.idle`
    and fail naturally on their next I/O (see the multiplexing design's Error handling section).

    Args:
      state: The `TransportState` to stop tracking.

    Returns:
      Whichever of its channels were sitting idle.
    """
    with self._ledger.lock:
      self._ledger.states.pop(id(state.transport), None)
      all_idle = self._ledger.idle.clear()
      orphaned: list[Channel] = []
      for c in all_idle:
        if c.state is state:
          orphaned.append(c)
          self._ledger.handle_states.pop(id(c.handle), None)
        else:
          self._ledger.idle.append(c)
      return orphaned

  def _validate(self, handle: SFTPClient) -> bool:
    """Checks whether a handle responds to a `listdir(".")` round trip.

    Args:
      handle: The handle to check.

    Returns:
      `True` if `handle` is still usable.
    """
    try:
      handle.listdir(".")
      return True
    except OSError as exc:
      # paramiko maps SFTP_PERMISSION_DENIED/SFTP_NO_SUCH_FILE status replies to IOError with
      # these errnos -- a non-listable root still means the server answered, so the channel is
      # fine. Any other OSError (socket reset, broken pipe, ...) means it isn't.
      return exc.errno in (errno.EACCES, errno.ENOENT)
    except SSHException, EOFError:
      # SSHException: protocol/transport failure. EOFError: listdir_attr() already swallows the
      # normal end-of-listing EOF internally, so one reaching here means the channel closed
      # mid-request. Either way the connection is unusable.
      return False

  def _close_quietly(self, handle: SFTPClient) -> None:
    """Best-effort close of a channel handle, swallowing any error.

    Args:
      handle: The handle to close.
    """
    try:
      handle.close()
    except Exception as e:
      logger.warning("Error closing SFTP channel handle: %s: %s", type(e).__name__, e)
      logger.debug("Traceback for SFTP channel handle close error", exc_info=e)

  def _close_quietly_transport(self, transport: Transport) -> None:
    """Best-effort close of a whole `Transport` via the connector, swallowing any error.

    Args:
      transport: The `Transport` to close.
    """
    try:
      self._connector.close_conn_handler(transport)
    except Exception as e:
      logger.warning("Error closing SFTP transport: %s: %s", type(e).__name__, e)
      logger.debug("Traceback for SFTP transport close error", exc_info=e)

  def _make_instrument(self, state: TransportState) -> IntrumentCallable:
    """Builds a per-checkout observer callback that feeds elapsed-time-weighted throughput samples
    into a `TransportState`.

    Args:
      state: The `TransportState` to feed throughput samples into.

    Returns:
      The observer callback.
    """
    last_sample = monotonic()

    def observer(data: bytes) -> None:
      """Records the bytes transferred since the last call as a throughput sample.

      Args:
        data: The chunk just transferred.
      """
      nonlocal last_sample
      now = monotonic()
      state.update_throughput(len(data), now - last_sample)
      last_sample = now

    return observer
