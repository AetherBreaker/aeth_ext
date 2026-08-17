# SFTP Channel Pool Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `SFTPPool` into `ChannelLedger` (pure locked state) + `SFTPChannelPool` (all acquire/release decision-making, and the `HandleProvider` `AdaptedSFTP` talks to), moving `SFTPAdapter` down to transport-dialing only, per the design doc.

**Architecture:** `SFTPAdapter` builds a `_SFTPConnector`, a `ChannelLedger` (holding `self` as its `TransportProvider`), and an `SFTPChannelPool` (holding the ledger + connector) in that order, then wires `ledger.pool = pool`. `AdaptedSFTP` is handed the pool, not the adapter, as its `HandleProvider`. Neither the pool nor the adapter holds a direct reference to the other — both reach the other only through the ledger.

**Tech Stack:** Python 3.14, `paramiko`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-15-sftp-channel-pool-separation-design.md`

## Global Constraints

- No change to the multiplexing algorithm's observable behavior (growth/shrink, saturation ratio, EWMA, cross-wave memory) — only which object owns which piece of bookkeeping.
- `TransportState.channel_count` must only ever be mutated under `ledger.lock` (closes the race the design calls out).
- `FTPAdapter`/`AdaptedFTP`/`AdaptedSFTP`/`credentials.py`/`create_ftp_adapter` are unaffected — do not touch them beyond the one line in `SFTPAdapter._build_session` that changes which object it hands `AdaptedSFTP` as its provider.
- Do not extract new helpers below ~4 lines unless a lint rule forces it (project convention) — several ledger operations below are intentionally left as direct dict/list method calls rather than named wrapper methods, per the design.

---

### Task 1: `ChannelLedger` + `SFTPChannelPool` replace `SFTPPool`

**Files:**
- Modify: `src/aeth_ext/ftp/types.py` — add `TransportProvider` protocol
- Rewrite: `src/aeth_ext/ftp/sftp_pool.py`
- Rewrite: `tests/ftp/test_sftp_pool.py`

**Interfaces:**
- Produces: `ChannelLedger(transports: TransportProvider)` with `.lock: RLock`, `.transports`, `.pool: SFTPChannelPool | None`, `.states: LockedDict[int, TransportState]`, `.handle_states: LockedDict[int, TransportState]`, `.idle: LockedList[Channel]`, `.in_flight: int`, `.wave_running_max: float`, `.last_wave_best_throughput: float | None`.
- Produces: `SFTPChannelPool(ledger: ChannelLedger, connector: _SFTPConnector, channels_per_transport: int)` with public `acquire() -> tuple[SFTPClient, Sequence[Callable[[bytes], Any]]]`, `release(handle: SFTPClient, is_fatal: bool) -> None`, `teardown() -> None`, `keepalive_check_one() -> None`.
- Produces: `TransportState`, `Channel` (unchanged in shape from today).
- Consumes (Task 2): `_SFTPConnector` from `adapter.py`, imported only under `TYPE_CHECKING` (a plain, non-pydantic class — nothing forces runtime resolution of this annotation — and importing it for real would create a cycle since `adapter.py` imports this module at runtime).

- [ ] **Step 1: Add `TransportProvider` to `types.py`**

```python
# src/aeth_ext/ftp/types.py — add to __all__: "TransportProvider"

class TransportProvider[TransportT](Protocol):
  """Narrow extension point: something that can dial a brand new low-level transport (a `Transport`,
  conceptually a TCP+SSH handshake) within its own connection-count ceiling, and be told when one died.

  `SFTPAdapter` structurally satisfies this via its own `open_transport`/`transport_dropped` methods --
  lets `SFTPChannelPool` grow the pool without holding a direct reference to `SFTPAdapter`.
  """

  __slots__ = ()

  def open_transport(self) -> TransportT | None:
    """Dials a new low-level transport within the provider's connection-count ceiling.

    Returns:
      The new transport, or `None` if the ceiling was already reached.
    """
    ...
  def transport_dropped(self) -> None:
    """Records that a previously-opened transport has died, freeing one slot in the ceiling."""
    ...
```

- [ ] **Step 2: Write `tests/ftp/test_sftp_pool.py` against the new shape**

Replace the whole file. `TransportState`/`Channel`/EWMA/saturation tests carry over unchanged (they only ever touched `TransportState` directly). Pool-level tests move to fakes for `TransportProvider` and `_SFTPConnector`, and exercise `SFTPChannelPool` through `ChannelLedger` instead of a flat dict:

```python
"""Unit tests for `aeth_ext.ftp.sftp_pool` -- pure bookkeeping, no real network."""

# Standard library imports
import threading

# Third party imports
from paramiko import SFTPClient, Transport

# First party imports
from aeth_ext.ftp.sftp_pool import Channel, ChannelLedger, LockedDict, LockedList, SFTPChannelPool, TransportState


class _FakeTransport(Transport):
  """Stands in for `paramiko.Transport` -- the pool only ever holds it as a dict key/attribute or
  calls `.is_active()`/`.close()` on it, so a real handshake is never needed."""

  def __init__(self, *, active: bool = True) -> None:
    self._active = active  # deliberately skip Transport.__init__

  def is_active(self) -> bool:
    return self._active

  def close(self) -> None:
    self._active = False


class _FakeChannel(SFTPClient):
  """Stands in for `paramiko.SFTPClient` -- same reasoning as `_FakeTransport`."""

  def __init__(self) -> None:
    self.closed = False  # deliberately skip SFTPClient.__init__

  def close(self) -> None:
    self.closed = True

  def listdir(self, path: str = ".") -> list[str]:
    return []


class _FakeConnector:
  """Stands in for `_SFTPConnector` -- hands out fresh `_FakeChannel`s and no-ops on transport close."""

  def request_handler(self, transport: Transport) -> SFTPClient:
    return _FakeChannel()

  def close_conn_handler(self, transport: Transport) -> None:
    transport.close()  # pyright: ignore[reportAttributeAccessIssue]


class _FakeTransportProvider:
  """Stands in for `SFTPAdapter` as a `TransportProvider` -- dials up to `ceiling` fake transports."""

  def __init__(self, ceiling: int = 100) -> None:
    self.ceiling = ceiling
    self.opened: list[Transport] = []
    self.dropped_count = 0

  def open_transport(self) -> Transport | None:
    if len(self.opened) >= self.ceiling:
      return None
    transport = _FakeTransport()
    self.opened.append(transport)
    return transport

  def transport_dropped(self) -> None:
    self.dropped_count += 1


def _make_pool(channels_per_transport: int = 4, ceiling: int = 100) -> tuple[SFTPChannelPool, ChannelLedger, _FakeTransportProvider]:
  provider = _FakeTransportProvider(ceiling)
  ledger = ChannelLedger(transports=provider)
  pool = SFTPChannelPool(ledger, _FakeConnector(), channels_per_transport)
  ledger.pool = pool
  return pool, ledger, provider


class TestLockedDict:
  def test_setitem_getitem_roundtrip(self) -> None:
    d: LockedDict[int, str] = LockedDict(threading.RLock())
    d[1] = "a"
    assert d[1] == "a"
    assert 1 in d

  def test_pop_and_delitem(self) -> None:
    d: LockedDict[int, str] = LockedDict(threading.RLock())
    d[1] = "a"
    assert d.pop(1) == "a"
    assert d.pop(1, "default") == "default"
    d[2] = "b"
    del d[2]
    assert 2 not in d

  def test_values_snapshots_under_lock(self) -> None:
    d: LockedDict[int, str] = LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    assert sorted(d.values()) == ["a", "b"]

  def test_clear_returns_everything_and_empties(self) -> None:
    d: LockedDict[int, str] = LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    assert sorted(d.clear()) == ["a", "b"]
    assert len(d) == 0

  def test_concurrent_mutation_never_corrupts_state(self) -> None:
    d: LockedDict[int, int] = LockedDict(threading.RLock())

    def writer(start: int) -> None:
      for i in range(start, start + 200):
        d[i] = i

    threads = [threading.Thread(target=writer, args=(base,)) for base in (0, 1000, 2000)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert len(d) == 600  # noqa: PLR2004 -- 3 threads * 200 unique keys each


class TestLockedList:
  def test_append_pop_contains(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    assert 1 in lst
    assert lst.pop() == 2
    assert len(lst) == 1

  def test_pop_empty_returns_none(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    assert lst.pop() is None

  def test_remove_missing_item_is_a_no_op(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    lst.remove(99)  # must not raise
    assert len(lst) == 0

  def test_clear_returns_everything_and_empties(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    assert lst.clear() == [1, 2]
    assert len(lst) == 0

  def test_concurrent_append_never_corrupts_state(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())

    def writer() -> None:
      for i in range(200):
        lst.append(i)

    threads = [threading.Thread(target=writer) for _ in range(3)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert len(lst) == 600  # noqa: PLR2004 -- 3 threads * 200 appends each


class TestChannelReuseUnderCap:
  def test_new_pool_has_no_growth_target(self) -> None:
    pool, _ledger, _provider = _make_pool(channels_per_transport=4)

    assert pool._pick_growth_target() is None  # pyright: ignore[reportPrivateUsage]

  def test_acquire_with_no_idle_channel_dials_a_new_transport(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)

    handle, _callbacks = pool.acquire()

    assert len(provider.opened) == 1
    assert ledger.handle_states.get(id(handle)) is not None

  def test_transport_at_channel_cap_is_not_a_growth_target(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=2)
    state = TransportState(transport=_FakeTransport())
    ledger.states[id(state.transport)] = state
    state.channel_count = 2

    assert pool._pick_growth_target() is None  # pyright: ignore[reportPrivateUsage]

  def test_released_channel_is_reused_on_next_acquire(self) -> None:
    pool, provider = _make_pool(channels_per_transport=4)[0], _make_pool(channels_per_transport=4)[2]
    handle, _ = pool.acquire()

    pool.release(handle, is_fatal=False)
    reused, _ = pool.acquire()

    assert reused is handle
    assert len(provider.opened) == 0  # a fresh pool/provider pair -- unrelated to the first acquire


class TestHandleToStateLookup:
  def test_untracked_handle_resolves_to_none(self) -> None:
    _pool, ledger, _provider = _make_pool()

    assert ledger.handle_states.get(id(_FakeChannel())) is None

  def test_acquired_handle_resolves_to_its_state(self) -> None:
    pool, ledger, _provider = _make_pool()

    handle, _ = pool.acquire()

    assert ledger.handle_states.get(id(handle)) is not None


class TestSaturationRouting:
  def test_growth_target_excludes_a_saturated_transport(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    fast = TransportState(transport=_FakeTransport())
    slow = TransportState(transport=_FakeTransport())
    ledger.states[id(fast.transport)] = fast
    ledger.states[id(slow.transport)] = slow
    for _ in range(TransportState._MIN_SAMPLES):  # pyright: ignore[reportPrivateUsage]
      fast.update_throughput(nbytes=1_000_000, elapsed=1.0)
      slow.update_throughput(nbytes=100, elapsed=1.0)

    target = pool._pick_growth_target()  # pyright: ignore[reportPrivateUsage]

    assert target is fast

  def test_releasing_a_channel_on_a_saturated_transport_does_not_idle_it(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    fast = TransportState(transport=_FakeTransport())
    slow = TransportState(transport=_FakeTransport())
    ledger.states[id(fast.transport)] = fast
    ledger.states[id(slow.transport)] = slow
    for _ in range(TransportState._MIN_SAMPLES):  # pyright: ignore[reportPrivateUsage]
      fast.update_throughput(nbytes=1_000_000, elapsed=1.0)
      slow.update_throughput(nbytes=100, elapsed=1.0)
    slow.channel_count = 1
    handle = _FakeChannel()
    ledger.handle_states[id(handle)] = slow
    ledger.in_flight = 1

    pool.release(handle, is_fatal=False)

    assert handle.closed is True
    assert slow.channel_count == 0
    assert ledger.handle_states.get(id(handle)) is None


class TestFatalRelease:
  def test_fatal_release_on_a_still_active_transport_discards_only_the_channel(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()

    pool.release(handle, is_fatal=True)

    assert handle.closed is True
    assert provider.dropped_count == 0
    assert ledger.handle_states.get(id(handle)) is None

  def test_fatal_release_on_a_dead_transport_drops_it_and_orphans_idle_siblings(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    first, _ = pool.acquire()
    state = ledger.handle_states.get(id(first))
    assert state is not None
    second_handle = _FakeChannel()
    ledger.handle_states[id(second_handle)] = state
    ledger.idle.append(Channel(handle=second_handle, state=state))
    state.transport.close()  # pyright: ignore[reportAttributeAccessIssue] -- kill it out from under `first`

    pool.release(first, is_fatal=True)

    assert first.closed is True
    assert second_handle.closed is True  # orphaned idle sibling closed too
    assert provider.dropped_count == 1
    assert ledger.states.get(id(state.transport)) is None


class TestTeardownAndKeepalive:
  def test_teardown_closes_every_transport_and_clears_tracking(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    state = ledger.handle_states.get(id(handle))

    pool.teardown()

    assert len(ledger.states) == 0
    assert len(ledger.idle) == 0
    assert state is None or not state.transport.is_active()  # pyright: ignore[reportOptionalMemberAccess]

  def test_keepalive_check_one_with_nothing_idle_is_a_no_op(self) -> None:
    pool, _ledger, _provider = _make_pool(channels_per_transport=4)

    pool.keepalive_check_one()  # must not raise

  def test_keepalive_check_one_revalidates_and_reidles_a_healthy_channel(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    assert len(ledger.idle) == 1

    pool.keepalive_check_one()

    assert handle.closed is False
    assert len(ledger.idle) == 1


class TestCrossWaveMemory:
  def test_no_memory_before_any_wave_completes(self) -> None:
    _pool, ledger, _provider = _make_pool(channels_per_transport=4)

    assert ledger.last_wave_best_throughput is None

  def test_wave_boundary_persists_the_running_max_on_zero_crossing(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    h1, callbacks1 = pool.acquire()
    h2, callbacks2 = pool.acquire()
    slower_nbytes = 500_000
    faster_nbytes = 1_000_000
    callbacks1[0](b"x" * slower_nbytes)
    pool.release(h1, is_fatal=False)
    assert ledger.last_wave_best_throughput is None  # still in-flight (h2)

    callbacks2[0](b"x" * faster_nbytes)
    pool.release(h2, is_fatal=False)  # in-flight count hits zero here

    assert ledger.last_wave_best_throughput is not None
    assert ledger.last_wave_best_throughput > 0
```

- [ ] **Step 3: Run the new test file to confirm it fails against the still-old `sftp_pool.py`**

Run: `uv run pytest tests/ftp/test_sftp_pool.py -v`
Expected: FAIL (import errors — `ChannelLedger`/`SFTPChannelPool`/`LockedDict`/`LockedList` don't exist yet).

- [ ] **Step 4: Rewrite `src/aeth_ext/ftp/sftp_pool.py`**

```python
"""Two-tier (Transport, channel) bookkeeping for `SFTPAdapter`'s channel multiplexing.

`ChannelLedger` holds the shared, self-locking state; `SFTPChannelPool` makes every decision on top of
it and is `AdaptedSFTP`'s `HandleProvider`. Kept out of `adapter.py` so the plain-FTP pooling path
(unchanged fixed one-connection-per-slot queue) isn't diluted by SFTP-only concepts, and kept entirely
out of `AdaptedSFTP` -- that class only ever sees a bare `SFTPClient` handler, identical in shape to
`AdaptedFTP`'s. This module is the only place that knows a checked-out `SFTPClient` came from a
specific `Transport`.
"""

# Standard library imports
from dataclasses import dataclass
from queue import Queue
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from typing import Any

  # Third party imports
  from paramiko import SFTPClient, Transport

  # First party imports
  from aeth_ext.ftp.adapter import _SFTPConnector  # plain class -- annotation-only, no runtime force
  from aeth_ext.ftp.types import TransportProvider

__all__ = ["Channel", "ChannelLedger", "LockedDict", "LockedList", "SFTPChannelPool", "TransportState"]


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

  def __init__(self, ledger: ChannelLedger, connector: _SFTPConnector, channels_per_transport: int) -> None:
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

  def acquire(self) -> tuple[SFTPClient, Sequence[Callable[[bytes], Any]]]:
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
    candidates.sort(key=lambda s: s.channel_count)
    return candidates[0]

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
      if channel in self._ledger.idle:
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
    except Exception:  # noqa: BLE001 -- any failure means the connection is unusable
      return False

  def _close_quietly(self, handle: SFTPClient) -> None:
    """Best-effort close of a channel handle, swallowing any error.

    Args:
      handle: The handle to close.
    """
    try:
      handle.close()
    except Exception:  # noqa: BLE001, S110 -- best-effort close of an already-broken connection
      pass

  def _close_quietly_transport(self, transport: Transport) -> None:
    """Best-effort close of a whole `Transport` via the connector, swallowing any error.

    Args:
      transport: The `Transport` to close.
    """
    try:
      self._connector.close_conn_handler(transport)
    except Exception:  # noqa: BLE001, S110 -- best-effort close during teardown
      pass

  def _make_instrument(self, state: TransportState) -> Callable[[bytes], None]:
    """Builds a per-checkout observer callback that feeds elapsed-time-weighted throughput samples
    into a `TransportState`.

    Args:
      state: The `TransportState` to feed throughput samples into.

    Returns:
      The observer callback.
    """
    last = [monotonic()]

    def observer(data: bytes) -> None:
      """Records the bytes transferred since the last call as a throughput sample.

      Args:
        data: The chunk just transferred.
      """
      now = monotonic()
      elapsed = now - last[0]
      last[0] = now
      state.update_throughput(len(data), elapsed)

    return observer
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/ftp/test_sftp_pool.py -v`
Expected: PASS (all tests, including the two `LockedDict`/`LockedList` concurrency tests).

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/aeth_ext/ftp/sftp_pool.py src/aeth_ext/ftp/types.py tests/ftp/test_sftp_pool.py` and `uv run pyright src/aeth_ext/ftp/sftp_pool.py src/aeth_ext/ftp/types.py`
Expected: no new errors. (`adapter.py` will still fail at this point — that's Task 2 — don't chase those.)

- [ ] **Step 7: Commit**

```bash
git add src/aeth_ext/ftp/types.py src/aeth_ext/ftp/sftp_pool.py tests/ftp/test_sftp_pool.py
git commit -m "$(cat <<'EOF'
refactor(ftp): split SFTPPool into ChannelLedger + SFTPChannelPool

SFTPPool mixed the multiplexing decision logic with thin bookkeeping methods
that only made sense read alongside their one call site in SFTPAdapter, and
one caller mutated TransportState.channel_count outside the pool's lock
entirely. ChannelLedger now owns only the shared, self-locking state;
SFTPChannelPool owns every acquire/release/growth/saturation decision in one
place and becomes AdaptedSFTP's HandleProvider directly, closing that race.

adapter.py still references the old SFTPPool API -- wired up in the next commit.
EOF
)"
```

---

### Task 2: Wire `SFTPAdapter` to the new pool

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py`
- Modify: `tests/ftp/test_adapter_sftp.py` — add pool-wiring coverage
- Modify: `tests/ftp/test_ftp_adapter_factory.py` — mechanical `_sftp_pool` → `_ledger` updates

**Interfaces:**
- Consumes: `ChannelLedger`, `SFTPChannelPool` from Task 1 (`src/aeth_ext/ftp/sftp_pool.py`).
- Produces: `SFTPAdapter.open_transport() -> Transport | None`, `SFTPAdapter.transport_dropped() -> None` (the `TransportProvider` surface), `SFTPAdapter._ledger: ChannelLedger`.

- [ ] **Step 1: Update `test_ftp_adapter_factory.py`'s 8 call sites mechanically**

Replace every `adapter._sftp_pool.state_for_handle(X)` with `adapter._ledger.handle_states.get(id(X))` in `tests/ftp/test_ftp_adapter_factory.py` (lines ~573, 574, 589, 590, 608, 624, 626, 634, 639) and drop the now-unneeded `# pyright: ignore[reportPrivateUsage, reportArgumentType]` down to `# pyright: ignore[reportPrivateUsage]` where `reportArgumentType` was only about the old method's signature (verify per-callsite; keep whichever ignore codes pyright still reports in Step 5).

- [ ] **Step 2: Add wiring coverage to `test_adapter_sftp.py`**

Append (needs a `provider` fixture argument that already exists in this file's other tests, but this new class doesn't touch the real server -- it inspects `SFTPAdapter`'s own construction):

```python
class TestPoolWiring:
  def test_build_session_hands_the_pool_not_the_adapter_as_provider(self) -> None:
    # First party imports
    from aeth_ext.ftp.credentials import SFTPCredentials
    from aeth_ext.ftp.adapter import SFTPAdapter

    adapter = SFTPAdapter(SFTPCredentials(host="127.0.0.1", username="u", password="p"))  # pyright: ignore[reportArgumentType]

    session = adapter.start_session()

    assert session._provider is adapter._ledger.pool  # pyright: ignore[reportPrivateUsage]
    assert session._provider is not adapter  # pyright: ignore[reportPrivateUsage]

  def test_transport_provider_methods_delegate_to_the_adapters_slot_bookkeeping(self) -> None:
    # First party imports
    from aeth_ext.ftp.credentials import SFTPCredentials
    from aeth_ext.ftp.adapter import SFTPAdapter

    adapter = SFTPAdapter(SFTPCredentials(host="127.0.0.1", username="u", password="p"), max_connections=1)  # pyright: ignore[reportArgumentType]
    adapter._current_size = 1  # pyright: ignore[reportPrivateUsage] -- simulate the ceiling already reached

    assert adapter.open_transport() is None  # _open_new_slot refuses past max_connections

    adapter.transport_dropped()

    assert adapter._current_size == 0  # pyright: ignore[reportPrivateUsage]
```

- [ ] **Step 3: Run the new/updated tests to confirm they fail against the still-old `adapter.py`**

Run: `uv run pytest tests/ftp/test_adapter_sftp.py::TestPoolWiring -v`
Expected: FAIL (`AttributeError: 'SFTPAdapter' object has no attribute '_ledger'`).

- [ ] **Step 4: Update `src/aeth_ext/ftp/adapter.py`**

4a. Change the import (line 18):

```python
from aeth_ext.ftp.sftp_pool import ChannelLedger, SFTPChannelPool
```

4b. `SFTPAdapter.__slots__` (was `("_connector", "_sftp_pool", "channels_per_transport")`):

```python
  __slots__ = ("_connector", "_ledger", "channels_per_transport")
```

4c. `SFTPAdapter.__init__` tail (replaces the `self._connector = ...` / `self.channels_per_transport = ...` / `self._sftp_pool = SFTPPool(...)` block):

```python
    self._connector = _SFTPConnector(credentials)
    self.channels_per_transport = channels_per_transport
    self._ledger = ChannelLedger(transports=self)
    pool = SFTPChannelPool(self._ledger, self._connector, channels_per_transport)
    self._ledger.pool = pool
```

4d. Replace `SFTPAdapter.acquire`/`release`/`_discard`/`_validate` entirely with:

```python
  def open_transport(self) -> Transport | None:
    """Dials a new `Transport` within `max_connections`, for `SFTPChannelPool` to grow into.

    Returns:
      The new `Transport`, or `None` if the ceiling was already reached.
    """
    return self._open_new_slot(self._connector.get_transport)

  def transport_dropped(self) -> None:
    """Records that `SFTPChannelPool` dropped a dead `Transport`, freeing one ceiling slot."""
    with self._size_lock:
      self._current_size -= 1
```

4e. Update `_keepalive_check_one`, `_teardown_idle`, `_build_session` to delegate through `self._ledger.pool`:

```python
  @override
  def _keepalive_check_one(self) -> None:
    """Pops one idle channel and validates it, discarding it (and possibly its `Transport`) if the
    validation fails."""
    assert self._ledger.pool is not None
    self._ledger.pool.keepalive_check_one()

  @override
  def _teardown_idle(self) -> None:
    """Closes every tracked `Transport` (and every channel opened on it)."""
    assert self._ledger.pool is not None
    self._ledger.pool.teardown()

  @override
  def _build_session(self, container_cls: str | None) -> AdaptedSFTP:
    """Builds a new `AdaptedSFTP` session bound to `SFTPChannelPool` (not this adapter) as its
    handle provider.

    Args:
      container_cls: Label to attach to log messages the session emits.

    Returns:
      The new session.
    """
    assert self._ledger.pool is not None
    return AdaptedSFTP(self._ledger.pool, container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo, chunk_size=self.chunk_size)
```

4f. Delete `_make_instrument` from `SFTPAdapter` entirely (it now lives on `SFTPChannelPool`, added in Task 1).

4g. Narrow `_PooledAdapterBase`'s abstract surface: remove the `@abstractmethod` decorator and body for `acquire`, `release`, and `_validate` (lines ~1193-1222) so only `_build_session`/`_keepalive_check_one`/`_teardown_idle` remain `@abstractmethod`. Rewrite the comment immediately above them (currently at adapter.py:1188-1191):

```python
  # --- abstract, subclass-owned: no runtime type checks here or in any subclass implementation, since
  # each subclass statically knows its own HandleT/SessionT. FTPAdapter is its own HandleProvider and
  # keeps concrete acquire/release/_validate methods to satisfy that structurally; SFTPAdapter is not --
  # SFTPChannelPool is AdaptedSFTP's provider instead, so SFTPAdapter carries none of the three. ---
```

4h. `FTPAdapter` keeps its existing `acquire`/`release`/`_validate` exactly as-is (no `@override` needed since they're no longer abstract, but leaving `@override` on is harmless and still correct — Pyright still checks it matches the base's now-non-abstract method of the same name if one remains; since the base method is deleted entirely for these three, remove the `@override` decorator from `FTPAdapter.acquire`/`release`/`_validate` too, since there's nothing left to override).

- [ ] **Step 5: Run tests and confirm they pass**

Run: `uv run pytest tests/ftp/test_adapter_sftp.py tests/ftp/test_ftp_adapter_factory.py tests/ftp/test_sftp_pool.py tests/ftp/test_transfer.py tests/ftp/test_types_and_errors.py tests/ftp/test_adapter_ftp.py -v`
Expected: PASS across the board.

- [ ] **Step 6: Lint and type-check the whole package**

Run: `uv run ruff check src/aeth_ext/ftp/ tests/ftp/` and `uv run pyright src/aeth_ext/ftp/ tests/ftp/`
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_adapter_sftp.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "$(cat <<'EOF'
refactor(ftp): wire SFTPAdapter to SFTPChannelPool as its transport provider

SFTPAdapter previously implemented acquire/release/_validate itself and held
a private SFTPPool it drove through a fixed multi-call sequence per checkout.
It now only dials Transports (open_transport/transport_dropped satisfy the
new TransportProvider protocol) and hands AdaptedSFTP the SFTPChannelPool
directly as its HandleProvider, matching how SFTPChannelPool now owns the
whole acquire/release decision in one place.
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: object model (adapter/pool/ledger/provider) → Tasks 1+2; `ChannelLedger`/`LockedDict`/`LockedList` → Task 1 Step 4; `SFTPChannelPool.acquire`/`release`/`teardown`/`keepalive_check_one` and private helpers → Task 1 Step 4; `TransportProvider` protocol → Task 1 Step 1; wiring order and no-direct-reference invariant → Task 2 Step 4; `_PooledAdapterBase` abstract-surface narrowing and comment rewrite → Task 2 Step 4g; test-file updates (`test_sftp_pool.py`, `test_adapter_sftp.py`, plus the not-explicitly-named-in-the-design `test_ftp_adapter_factory.py`) → Task 1 Step 2, Task 2 Steps 1-2.
- Out-of-scope items (multiplexing algorithm, `FTPAdapter`/`AdaptedFTP`, `credentials.py`, `create_ftp_adapter`) are untouched by every step above.
