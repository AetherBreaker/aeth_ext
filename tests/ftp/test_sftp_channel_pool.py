"""Unit tests for `aeth_ext.ftp.pool.sftp_channel_pool` -- pure bookkeeping, no real network."""

# Standard library imports
import threading
from time import monotonic, sleep
from typing import override

# Third party imports
import pytest
from paramiko import SFTPClient, Transport

# First party imports
from aeth_ext.ftp.errors import PoolClosedError
from aeth_ext.ftp.pool.base import WakeupGate
from aeth_ext.ftp.pool.sftp_channel_pool import (
  Channel,
  ChannelLedger,
  SFTPChannelPool,
  TransportState,
  _LockedDict,  # pyright: ignore[reportPrivateUsage]
  _LockedList,  # pyright: ignore[reportPrivateUsage]
)
from tests.ftp.conftest import wait_until


class _FakeTransport(Transport):
  """Stands in for `paramiko.Transport` -- the pool only ever holds it as a dict key/attribute or
  calls `.is_active()`/`.close()` on it, so a real handshake is never needed."""

  def __init__(self, *, active: bool = True) -> None:
    self._active = active  # deliberately skip Transport.__init__

  @override
  def is_active(self) -> bool:
    return self._active

  @override
  def close(self) -> None:
    self._active = False


class _FakeChannel(SFTPClient):
  """Stands in for `paramiko.SFTPClient` -- same reasoning as `_FakeTransport`."""

  def __init__(self) -> None:
    self.closed = False  # deliberately skip SFTPClient.__init__
    self.fail_listdir = False

  @override
  def close(self) -> None:
    self.closed = True

  @override
  def listdir(self, path: str = ".") -> list[str]:
    # `_validate()` round-trips through listdir("."), so this flag is how a test makes an idle
    # channel fail revalidation. A plain OSError carries no errno, which _validate() reads as dead.
    if self.fail_listdir:
      raise OSError("simulated dead channel")
    return []


class _FakeConnector:
  """Stands in for `SFTPConnector` -- hands out fresh `_FakeChannel`s and closes channels/transports
  by delegating to their own `.close()`."""

  def request_handler(self, transport: Transport) -> SFTPClient:
    return _FakeChannel()

  def close_conn_handler(self, handle: SFTPClient) -> None:
    handle.close()

  def close_transport_handler(self, handle: Transport) -> None:
    handle.close()


class _BarrierConnector(_FakeConnector):
  """Delays `request_handler` until every racing thread has passed the growth-target decision, so
  concurrent `acquire()` calls are forced to overlap instead of serializing by accident."""

  def __init__(self, barrier: threading.Barrier) -> None:
    self._barrier = barrier

  @override
  def request_handler(self, transport: Transport) -> SFTPClient:
    self._barrier.wait(timeout=5)
    return super().request_handler(transport)


class _FailingConnector(_FakeConnector):
  """Fails `request_handler` a fixed number of times before succeeding."""

  def __init__(self, fail_times: int = 1) -> None:
    self._fail_times = fail_times
    self.calls = 0

  @override
  def request_handler(self, transport: Transport) -> SFTPClient:
    self.calls += 1
    if self.calls <= self._fail_times:
      raise OSError("simulated channel-open failure")
    return super().request_handler(transport)


class _SiblingRaceConnector(_FakeConnector):
  """Holds the *first* `request_handler` call open until a sibling has reserved a second channel on
  the very `Transport` it is opening on, then fails it; every later call succeeds. Recreates the
  window where one caller's channel-open is still in flight while another multiplexes onto the
  `Transport` it dialed."""

  def __init__(self) -> None:
    self.ledger: ChannelLedger | None = None  # assigned once _make_pool has built one
    self._calls = 0
    self._lock = threading.Lock()

  @override
  def request_handler(self, transport: Transport) -> SFTPClient:
    with self._lock:
      self._calls += 1
      is_first = self._calls == 1
    if not is_first:
      return super().request_handler(transport)
    assert self.ledger is not None, "assign .ledger before using this connector"
    deadline = monotonic() + 5.0
    while monotonic() < deadline:
      state = self.ledger.states.get(id(transport))
      if state is not None and state.channel_count >= 2:  # noqa: PLR2004 -- this caller's own slot, plus the sibling's
        break
      sleep(0.005)
    raise OSError("simulated channel-open failure")


class _FakeTransportProvider:
  """Stands in for `TransportDialer` -- dials up to `ceiling` fake transports. Duck-typed rather than a
  real `TransportDialer`, since `TransportDialer` wraps a real `PooledAdapterBase`'s bookkeeping that
  this pure-bookkeeping test suite has no need to construct."""

  def __init__(self, ceiling: int = 100) -> None:
    self.ceiling = ceiling
    self.opened: list[Transport] = []
    self.dropped_count = 0

  def open_transport(self) -> Transport | None:
    # Mirrors the real TransportDialer/_current_size: the ceiling caps *currently live* transports,
    # not the lifetime total -- a dropped transport frees a slot for a later open() to reuse.
    if len(self.opened) - self.dropped_count >= self.ceiling:
      return None
    transport = _FakeTransport()
    self.opened.append(transport)
    return transport

  def transport_dropped(self) -> None:
    self.dropped_count += 1


def _make_pool(
  channels_per_transport: int = 4,
  ceiling: int = 100,
  connector: _FakeConnector | None = None,
  wakeup: WakeupGate | None = None,
) -> tuple[SFTPChannelPool, ChannelLedger, _FakeTransportProvider]:
  # `wakeup` is normally the owning SFTPAdapter's gate; tests that need to close it or observe
  # wakeups pass their own, everyone else gets a throwaway.
  provider = _FakeTransportProvider(ceiling)
  ledger = ChannelLedger(transports=provider)  # pyright: ignore[reportArgumentType] -- duck-typed fake, see _FakeTransportProvider's docstring
  pool = SFTPChannelPool(
    ledger,
    connector or _FakeConnector(),  # pyright: ignore[reportArgumentType] -- duck-typed fake, see _FakeConnector's docstring
    channels_per_transport,
    wakeup or WakeupGate(),
    lambda: None,
  )
  ledger.pool = pool
  return pool, ledger, provider


class TestLockedDict:
  def test_setitem_getitem_roundtrip(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    assert d[1] == "a"
    assert 1 in d

  def test_pop_and_delitem(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    assert d.pop(1) == "a"
    assert d.pop(1, "default") == "default"
    d[2] = "b"
    del d[2]
    assert 2 not in d  # noqa: PLR2004

  def test_values_snapshots_under_lock(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    assert sorted(d.values()) == ["a", "b"]

  def test_clear_empties(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    d.clear()
    assert len(d) == 0

  def test_concurrent_mutation_never_corrupts_state(self) -> None:
    d: _LockedDict[int, int] = _LockedDict(threading.RLock())

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
    lst: _LockedList[int] = _LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    assert 1 in lst
    assert lst.pop() == 2  # noqa: PLR2004
    assert len(lst) == 1

  def test_pop_empty_raises_index_error(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    with pytest.raises(IndexError):
      lst.pop()

  def test_remove_missing_item_raises_value_error(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    with pytest.raises(ValueError, match="not in list"):
      lst.remove(99)

  def test_copy_snapshots_without_mutating(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    assert lst.copy() == [1, 2]
    assert len(lst) == 2  # noqa: PLR2004

  def test_clear_empties(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    lst.clear()
    assert len(lst) == 0

  def test_concurrent_append_never_corrupts_state(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())

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
    pool, _ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()

    pool.release(handle, is_fatal=False)
    reused, _ = pool.acquire()

    assert reused is handle
    assert len(provider.opened) == 1  # no second Transport dialed -- the idle channel was reused


class TestGrowthReservationRace:
  def test_concurrent_acquires_never_exceed_channels_per_transport(self) -> None:
    channels_per_transport = 3
    thread_count = 8  # comfortably more than the cap, to force contention over one transport
    barrier = threading.Barrier(thread_count)
    pool, ledger, _provider = _make_pool(channels_per_transport=channels_per_transport, connector=_BarrierConnector(barrier))
    max_observed = 0
    lock = threading.Lock()

    def worker() -> None:
      nonlocal max_observed
      pool.acquire()
      with lock:
        max_observed = max(max_observed, *(s.channel_count for s in ledger.states.values()))

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert max_observed <= channels_per_transport

  def test_failed_request_handler_on_an_existing_transport_rolls_back_the_channel_count(self) -> None:
    # Pre-register an existing transport with spare capacity so _pick_growth_target() selects it
    # instead of dialing a new one -- this isolates the plain rollback path (see
    # TestNewTransportFailureCleanup for the freshly-dialed-transport teardown path).
    pool, ledger, provider = _make_pool(channels_per_transport=4, connector=_FailingConnector(fail_times=1))
    existing = TransportState(transport=_FakeTransport(), channel_count=1)
    ledger.states[id(existing.transport)] = existing

    with pytest.raises(OSError, match="simulated"):
      pool.acquire()

    assert ledger.states.get(id(existing.transport)) is existing  # still tracked, not torn down
    assert existing.channel_count == 1  # reserved to 2, rolled back to its pre-acquire count
    assert len(ledger.handle_states) == 0
    assert ledger.in_flight == 0
    assert provider.dropped_count == 0
    assert len(provider.opened) == 0  # growth reused the existing transport -- nothing new was dialed

    # A retried acquire() succeeds and reuses the same transport rather than leaking a second one.
    handle, _ = pool.acquire()
    assert ledger.handle_states.get(id(handle)) is not None
    assert existing.channel_count == 2  # noqa: PLR2004


class TestNewTransportFailureCleanup:
  def test_failed_request_handler_on_a_freshly_dialed_transport_tears_it_down(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4, connector=_FailingConnector(fail_times=1))

    with pytest.raises(OSError, match="simulated"):
      pool.acquire()

    assert len(ledger.states) == 0  # deregistered, not left counted forever
    assert len(ledger.handle_states) == 0
    assert ledger.in_flight == 0
    assert provider.dropped_count == 1  # _current_size corrected back down
    assert len(provider.opened) == 1
    assert provider.opened[0].is_active() is False  # actually closed, not just deregistered

    # A retried acquire() dials a second, independent transport rather than reusing the dead one.
    handle, _ = pool.acquire()
    assert len(provider.opened) == 2  # noqa: PLR2004
    assert ledger.handle_states.get(id(handle)) is not None


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

  def test_releasing_the_last_channel_on_a_saturated_transport_drops_it(self) -> None:
    """A saturated Transport popped down to zero channels must not stay registered -- otherwise it
    permanently occupies a _current_size slot with nothing left to ever update its EWMA or admit a
    replacement Transport."""
    pool, ledger, provider = _make_pool(channels_per_transport=4)
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

    assert id(slow.transport) not in ledger.states
    assert provider.dropped_count == 1


class TestFatalRelease:
  def test_fatal_release_on_a_still_active_transport_discards_only_the_channel(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()

    pool.release(handle, is_fatal=True)

    assert handle.closed is True  # pyright: ignore[reportAttributeAccessIssue]
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
    state.transport.close()  # kill it out from under `first`

    pool.release(first, is_fatal=True)

    assert first.closed is True  # pyright: ignore[reportAttributeAccessIssue]
    assert second_handle.closed is True  # orphaned idle sibling closed too
    assert provider.dropped_count == 1
    assert ledger.states.get(id(state.transport)) is None
    assert ledger.handle_states.get(id(first)) is None  # the released channel itself, not just the sibling
    assert ledger.in_flight == 0

  def test_two_checked_out_siblings_on_a_dead_transport_drop_the_transport_only_once(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    first, _ = pool.acquire()
    state = ledger.handle_states.get(id(first))
    assert state is not None
    second_handle = _FakeChannel()
    ledger.handle_states[id(second_handle)] = state
    state.channel_count += 1
    ledger.in_flight += 1
    state.transport.close()  # kill it out from under both

    pool.release(first, is_fatal=True)
    pool.release(second_handle, is_fatal=True)

    assert provider.dropped_count == 1
    assert ledger.handle_states.get(id(first)) is None
    assert ledger.handle_states.get(id(second_handle)) is None
    assert ledger.in_flight == 0


class TestEmptyTransportExpiry:
  """`_discard` (a single channel failing validation while its Transport is still active) leaves an
  emptied Transport registered for reuse rather than closing it immediately -- a single shared prune
  thread bounds how long it's allowed to sit open with nothing on it, closing it
  `_EMPTY_TRANSPORT_TTL` seconds after its most recent `last_released` stamp."""

  def test_discarding_the_last_channel_leaves_the_transport_open_until_it_expires(
    self, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    monkeypatch.setattr(SFTPChannelPool, "_EMPTY_TRANSPORT_TTL", 0.05)
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    state = ledger.handle_states.get(id(handle))
    assert state is not None

    pool.release(handle, is_fatal=True)  # still active -- routes to _discard, not the dead cascade

    # Not closed synchronously: the Transport gets a grace period to be reused.
    assert id(state.transport) in ledger.states
    assert provider.dropped_count == 0
    assert state.transport.is_active() is True

    assert wait_until(lambda: id(state.transport) not in ledger.states, timeout=2.0)
    assert provider.dropped_count == 1
    assert state.transport.is_active() is False

  def test_reserving_a_channel_before_expiry_keeps_the_transport_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SFTPChannelPool, "_EMPTY_TRANSPORT_TTL", 0.05)
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    state = ledger.handle_states.get(id(handle))
    assert state is not None

    pool.release(handle, is_fatal=True)
    handle2, _ = pool.acquire()  # reserves on the same still-registered, still-active Transport

    sleep(0.1)  # past the original TTL

    assert id(state.transport) in ledger.states
    assert provider.dropped_count == 0
    pool.release(handle2, is_fatal=False)

  def test_re_emptying_before_expiry_resets_the_grace_period(self, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale wake computed for the first empty spell must not close a Transport that was reused
    and emptied again by a second, later spell -- the second spell gets its own full TTL, since the
    prune thread always re-checks `last_released` fresh rather than trusting the deadline it woke up
    for."""
    monkeypatch.setattr(SFTPChannelPool, "_EMPTY_TRANSPORT_TTL", 0.15)
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    state = ledger.handle_states.get(id(handle))
    assert state is not None

    pool.release(handle, is_fatal=True)  # first empty spell -- naive deadline ~0.15s
    sleep(0.08)
    handle2, _ = pool.acquire()  # reused before the first spell's deadline
    sleep(0.08)
    pool.release(handle2, is_fatal=True)  # second empty spell at ~0.16s -- real deadline ~0.31s

    sleep(0.12)  # ~0.28s total: the first spell's deadline has long passed, the second's hasn't yet
    assert id(state.transport) in ledger.states, "closed on the first spell's stale deadline"
    assert provider.dropped_count == 0

    assert wait_until(lambda: id(state.transport) not in ledger.states, timeout=2.0)
    assert provider.dropped_count == 1

  def test_multiple_empty_transports_share_a_single_prune_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three Transports emptying independently must be serviced by one prune thread, not one per
    empty event -- the whole point of waiting on the single oldest deadline and re-sweeping."""
    monkeypatch.setattr(SFTPChannelPool, "_EMPTY_TRANSPORT_TTL", 60.0)  # never actually fires here
    pool, ledger, provider = _make_pool(channels_per_transport=1)  # forces 3 distinct Transports
    handles = [pool.acquire()[0] for _ in range(3)]

    assert pool._prune_thread is None  # pyright: ignore[reportPrivateUsage] -- nothing emptied yet

    for handle in handles:
      pool.release(handle, is_fatal=True)

    thread = pool._prune_thread  # pyright: ignore[reportPrivateUsage]
    assert thread is not None
    assert thread.is_alive()
    assert len(ledger.states) == 3  # noqa: PLR2004 -- all three left registered, none closed
    assert provider.dropped_count == 0

  def test_closing_the_pool_wakes_the_prune_thread_instead_of_it_sleeping_out_the_ttl(self) -> None:
    """The prune thread must retire promptly when the pool closes mid-wait, not sleep out the rest
    of a (potentially long) TTL pinning the pool's object graph alive for no reason -- teardown()
    closes every currently-empty Transport itself, so there's nothing left for the pruner to do."""
    wakeup = WakeupGate()
    pool, _ledger, provider = _make_pool(channels_per_transport=4, wakeup=wakeup)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=True)  # still active -- schedules the pruner with a long TTL

    thread = pool._prune_thread  # pyright: ignore[reportPrivateUsage]
    assert thread is not None
    assert thread.is_alive()

    wakeup.close()

    assert wait_until(lambda: not thread.is_alive(), timeout=2.0), "prune thread did not wake on close()"
    # Nothing pruned by the thread itself -- it just retired and left the Transport for teardown().
    assert provider.dropped_count == 0


class TestTeardownAndKeepalive:
  def test_teardown_closes_every_transport_and_clears_tracking(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    state = ledger.handle_states.get(id(handle))

    pool.teardown()

    assert len(ledger.states) == 0
    assert len(ledger.idle) == 0
    assert state is None or not state.transport.is_active()

  def test_teardown_leaves_a_checked_out_channel_and_its_transport_releasable(self) -> None:
    """A session still running when shutdown happens must be able to release() normally afterward,
    not hit release()'s `assert state is not None` against tracking teardown() already wiped."""
    # channels_per_transport=1 forces the second acquire() onto a separate Transport, rather than
    # multiplexing both handles onto the first one.
    wakeup = WakeupGate()
    pool, ledger, provider = _make_pool(channels_per_transport=1, wakeup=wakeup)
    checked_out, _ = pool.acquire()
    idle_handle, _ = pool.acquire()
    pool.release(idle_handle, is_fatal=False)  # goes to ledger.idle, on a separate Transport
    assert len(ledger.idle) == 1
    assert len(ledger.states) == 2  # noqa: PLR2004 -- one Transport per acquire() above

    # PooledAdapterBase._shutdown_teardown always closes the gate before calling this; done
    # explicitly here since this test drives SFTPChannelPool.teardown() directly.
    wakeup.close()
    pool.teardown()

    # The idle Transport (nothing checked out on it) is closed and dropped...
    assert len(ledger.idle) == 0
    assert provider.dropped_count == 1
    # ...but the still-checked-out channel's Transport stays registered and releasable.
    state = ledger.handle_states.get(id(checked_out))
    assert state is not None
    assert len(ledger.states) == 1

    pool.release(checked_out, is_fatal=False)  # must not raise release()'s "must have been tracked" assert

    # A clean release on a closed pool must close the Transport outright, not park it in
    # ledger.idle -- nothing will ever drain that again post-teardown, which would leak it forever.
    assert len(ledger.idle) == 0
    assert len(ledger.states) == 0
    assert provider.dropped_count == 2  # noqa: PLR2004 -- both Transports now closed

  def test_keepalive_check_one_with_nothing_idle_is_a_no_op(self) -> None:
    pool, _ledger, _provider = _make_pool(channels_per_transport=4)

    pool.keepalive_check_one()  # must not raise

  def test_keepalive_check_one_revalidates_and_reidles_a_healthy_channel(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    assert len(ledger.idle) == 1

    pool.keepalive_check_one()

    assert handle.closed is False  # pyright: ignore[reportAttributeAccessIssue]
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


class TestBlockedAcquireWakesOnCapacityFreed:
  def test_dead_transport_release_at_full_capacity_unblocks_a_waiting_acquire(self) -> None:
    # channels_per_transport=1 and ceiling=1 together mean exactly one channel can exist at a time --
    # the only way the pool ever has spare capacity again is a transport actually dying and being
    # dropped, never an idle channel appearing in ledger.idle.
    pool, ledger, provider = _make_pool(channels_per_transport=1, ceiling=1)
    first, _ = pool.acquire()
    state = ledger.handle_states.get(id(first))
    assert state is not None

    got_second: list[SFTPClient] = []
    unblocked = threading.Event()

    def _acquire_second() -> None:
      second, _ = pool.acquire()
      got_second.append(second)
      unblocked.set()

    t = threading.Thread(target=_acquire_second, daemon=True)
    t.start()
    assert not unblocked.wait(timeout=0.3), "second acquire should still be blocked -- pool is at capacity"

    state.transport.close()  # kill it out from under `first`
    # A dead-transport release frees _current_size without ever putting anything into ledger.idle --
    # before the fix, a waiter blocked on an idle-only retry loop would never learn about it.
    pool.release(first, is_fatal=True)

    assert unblocked.wait(timeout=2), "a dead-transport release must wake a blocked acquire, not hang forever"
    t.join(timeout=2)
    assert len(got_second) == 1
    assert provider.dropped_count == 1


class TestThroughputInstrumentConcurrency:
  def test_concurrent_channels_on_one_transport_serialize_through_the_ledger_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each observer has its own private `last_sample` closure (one per checked-out channel), but all
    # of them feed the same shared TransportState -- exactly what channels_per_transport > 1 allows in
    # production, and the scenario update_throughput()'s unsynchronized read-modify-write corrupts.
    #
    # A plain "call N times from N threads, assert the final count" doesn't reliably reproduce a lost
    # update -- the vulnerable window is a couple of bytecodes, too narrow to hit by luck under the
    # GIL's default switch interval. Instead, patch update_throughput to sleep partway through and
    # measure how many calls are ever inside it at once: with the ledger lock held across the call
    # (the fix), that's always 1 regardless of the sleep; without it, concurrent observers overlap.
    pool, _ledger, _provider = _make_pool(channels_per_transport=8)
    state = TransportState(transport=_FakeTransport())
    observer_count = 8
    observers = [pool._make_instrument(state) for _ in range(observer_count)]  # pyright: ignore[reportPrivateUsage]

    concurrent_count = 0
    max_concurrent = 0
    counter_lock = threading.Lock()
    original_update = TransportState.update_throughput

    def spy_update(self: TransportState, nbytes: int, elapsed: float) -> None:
      nonlocal concurrent_count, max_concurrent
      with counter_lock:
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
      sleep(0.01)
      original_update(self, nbytes, elapsed)
      with counter_lock:
        concurrent_count -= 1

    monkeypatch.setattr(TransportState, "update_throughput", spy_update)

    threads = [threading.Thread(target=observer, args=(b"x",)) for observer in observers]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert max_concurrent == 1
    assert state.sample_count == observer_count


class TestColdStartConsolidation:
  def test_concurrent_cold_start_acquires_fill_transports_instead_of_dialing_one_each(self) -> None:
    # Every caller starts from "nothing to grow onto" and only re-picks a target *inside* the dial
    # lock. Without that re-pick each one acts on its stale answer and dials its own Transport, and
    # the pool settles at one single-channel Transport per caller -- _pick_growth_target() prefers the
    # lowest channel_count, so later growth keeps spreading across them instead of filling them.
    channels_per_transport = 4
    thread_count = 8
    expected_transports = 2  # 8 channels at a cap of 4 needs exactly two, whatever the interleaving
    barrier = threading.Barrier(thread_count)
    pool, _ledger, provider = _make_pool(channels_per_transport=channels_per_transport, connector=_BarrierConnector(barrier))

    threads = [threading.Thread(target=pool.acquire) for _ in range(thread_count)]
    for t in threads:
      t.start()
    for t in threads:
      t.join(timeout=10)

    assert all(not t.is_alive() for t in threads)
    assert len(provider.opened) == expected_transports


class TestSiblingSurvivesFailedChannelOpen:
  def test_a_failed_first_open_leaves_a_siblings_transport_intact(self) -> None:
    connector = _SiblingRaceConnector()
    pool, ledger, provider = _make_pool(channels_per_transport=4, connector=connector)
    connector.ledger = ledger
    failed: list[Exception] = []
    acquired: list[SFTPClient] = []

    def _dials_then_fails() -> None:
      try:
        pool.acquire()
      except Exception as exc:  # noqa: BLE001 -- deliberately broad: the assertion below is what pins the type
        failed.append(exc)

    def _multiplexes_onto_it() -> None:
      handle, _ = pool.acquire()
      acquired.append(handle)

    dialer = threading.Thread(target=_dials_then_fails, daemon=True)
    dialer.start()
    # Only start the sibling once the dialed Transport is registered, so it deterministically
    # multiplexes onto that one instead of racing ahead and dialing its own.
    assert wait_until(lambda: len(ledger.states) == 1)
    sibling = threading.Thread(target=_multiplexes_onto_it, daemon=True)
    sibling.start()
    dialer.join(timeout=10)
    sibling.join(timeout=10)

    assert len(failed) == 1
    assert isinstance(failed[0], OSError)
    assert len(acquired) == 1
    assert len(provider.opened) == 1
    # Rollback must key off "did that leave the Transport at zero channels", not "did I dial it":
    # the sibling reserved a slot while the failing open was in flight and is still using it.
    state = ledger.states.get(id(provider.opened[0]))
    assert state is not None
    assert state.channel_count == 1
    assert provider.dropped_count == 0
    assert provider.opened[0].is_active() is True
    assert ledger.handle_states.get(id(acquired[0])) is state


class TestFailedRevalidationCascade:
  def test_a_dead_transport_behind_a_failed_revalidation_is_dropped_whole(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)  # idle, so the next acquire revalidates it
    state = ledger.handle_states.get(id(handle))
    assert state is not None
    handle.fail_listdir = True  # pyright: ignore[reportAttributeAccessIssue]
    dead_transport = state.transport
    dead_transport.close()

    replacement, _ = pool.acquire()

    # Discarding the channel alone would leave the dead Transport registered, still holding a
    # _current_size slot, until some later growth attempt picked it and handed its failure to an
    # unlucky caller. Routing the failure through release() cascades to the whole Transport instead.
    assert ledger.states.get(id(dead_transport)) is None
    assert provider.dropped_count == 1
    assert ledger.handle_states.get(id(replacement)) is not None

  def test_a_live_transport_behind_a_failed_revalidation_keeps_only_its_channel_dropped(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    state = ledger.handle_states.get(id(handle))
    assert state is not None
    handle.fail_listdir = True  # pyright: ignore[reportAttributeAccessIssue]

    replacement, _ = pool.acquire()

    # The counterpart that keeps the cascade honest: one bad channel on a healthy Transport must not
    # take the Transport (and its siblings) down with it.
    assert ledger.states.get(id(state.transport)) is state
    assert provider.dropped_count == 0
    assert len(provider.opened) == 1
    assert replacement is not handle
    assert ledger.handle_states.get(id(replacement)) is state


class TestPoolLevelTerminalClose:
  def test_acquire_after_close_raises_even_though_an_idle_channel_is_available(self) -> None:
    gate = WakeupGate()
    pool, ledger, _provider = _make_pool(channels_per_transport=4, wakeup=gate)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    assert len(ledger.idle) == 1

    gate.close()

    # retry_until() is only reached once a checkout comes up empty, so a gate-internal check alone
    # would let this acquire sail straight through a torn-down pool on the idle channel.
    with pytest.raises(PoolClosedError):
      pool.acquire()

  def test_release_after_close_closes_an_already_checked_out_channel_instead_of_pooling_it(self) -> None:
    """release() after close() must not raise -- teardown is one-way for acquire only, and a
    checked-out session must be able to finish and release normally. But it also must not queue the
    handle into ledger.idle: nothing ever drains that again once the gate is closed, so idling it
    here would leak the Transport (and its live background thread) for the rest of the process."""
    gate = WakeupGate()
    pool, ledger, provider = _make_pool(channels_per_transport=4, wakeup=gate)
    handle, _ = pool.acquire()

    gate.close()
    pool.release(handle, is_fatal=False)  # must not raise

    assert len(ledger.idle) == 0
    assert len(ledger.states) == 0
    assert provider.dropped_count == 1


class TestWavePeakRetention:
  def test_the_wave_max_keeps_an_early_peak_after_the_ewma_collapses(self, monkeypatch: pytest.MonkeyPatch) -> None:
    # Scripted clock, consumed in order by _make_instrument()'s initial reading plus one per observer
    # call: one fast chunk over 0.1s, then ten 1-byte chunks 100s apart -- enough for the EWMA
    # (alpha 0.3, so a 0.7 decay per sample) to fall two orders of magnitude below the peak.
    tail_samples = 10
    times = iter([0.0, 0.1, *(0.1 + 100.0 * (i + 1) for i in range(tail_samples))])
    monkeypatch.setattr("aeth_ext.ftp.pool.sftp_channel_pool.monotonic", lambda: next(times))
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    peak_rate = 10_000_000.0  # 1_000_000 bytes over 0.1s

    handle, callbacks = pool.acquire()
    callbacks[0](bytes(1_000_000))
    for _ in range(tail_samples):
      callbacks[0](b"x")

    state = ledger.handle_states.get(id(handle))
    assert state is not None
    assert state.ewma_throughput is not None
    # Folding the peak in per sample rather than once at release is what preserves it: the wave's max
    # is the *next* wave's saturation baseline, and an understated one makes healthy Transports look
    # saturated and grow past unnecessarily.
    assert ledger.wave_running_max == pytest.approx(peak_rate)
    assert ledger.wave_running_max > state.ewma_throughput
