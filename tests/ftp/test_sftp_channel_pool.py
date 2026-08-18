"""Unit tests for `aeth_ext.ftp.pool.sftp_channel_pool` -- pure bookkeeping, no real network."""

# Standard library imports
import threading
from typing import override

# Third party imports
import pytest
from paramiko import SFTPClient, Transport

# First party imports
from aeth_ext.ftp.pool.sftp_channel_pool import (
  Channel,
  ChannelLedger,
  SFTPChannelPool,
  TransportState,
  _LockedDict,  # pyright: ignore[reportPrivateUsage]
  _LockedList,  # pyright: ignore[reportPrivateUsage]
)


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

  @override
  def close(self) -> None:
    self.closed = True

  @override
  def listdir(self, path: str = ".") -> list[str]:
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


class _FakeTransportProvider:
  """Stands in for `TransportDialer` -- dials up to `ceiling` fake transports. Duck-typed rather than a
  real `TransportDialer`, since `TransportDialer` wraps a real `PooledAdapterBase`'s bookkeeping that
  this pure-bookkeeping test suite has no need to construct."""

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


def _make_pool(
  channels_per_transport: int = 4, ceiling: int = 100, connector: _FakeConnector | None = None
) -> tuple[SFTPChannelPool, ChannelLedger, _FakeTransportProvider]:
  provider = _FakeTransportProvider(ceiling)
  ledger = ChannelLedger(transports=provider)  # pyright: ignore[reportArgumentType] -- duck-typed fake, see _FakeTransportProvider's docstring
  pool = SFTPChannelPool(ledger, connector or _FakeConnector(), channels_per_transport)  # pyright: ignore[reportArgumentType] -- duck-typed fake, see _FakeConnector's docstring
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
