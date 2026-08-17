"""Unit tests for `aeth_ext.ftp.sftp_pool` -- pure bookkeeping, no real network."""

# Standard library imports
import threading
from typing import override

# Third party imports
import pytest
from paramiko import SFTPClient, Transport

# First party imports
from aeth_ext.ftp.sftp_pool import Channel, ChannelLedger, LockedDict, LockedList, SFTPChannelPool, TransportState


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
  """Stands in for `_SFTPConnector` -- hands out fresh `_FakeChannel`s and no-ops on transport close."""

  def request_handler(self, transport: Transport) -> SFTPClient:
    return _FakeChannel()

  def close_conn_handler(self, handle: Transport) -> None:
    handle.close()


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
    assert 2 not in d  # noqa: PLR2004

  def test_values_snapshots_under_lock(self) -> None:
    d: LockedDict[int, str] = LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    assert sorted(d.values()) == ["a", "b"]

  def test_clear_empties(self) -> None:
    d: LockedDict[int, str] = LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    d.clear()
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
    assert lst.pop() == 2  # noqa: PLR2004
    assert len(lst) == 1

  def test_pop_empty_raises_index_error(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    with pytest.raises(IndexError):
      lst.pop()

  def test_remove_missing_item_raises_value_error(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    with pytest.raises(ValueError, match="not in list"):
      lst.remove(99)

  def test_copy_snapshots_without_mutating(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    assert lst.copy() == [1, 2]
    assert len(lst) == 2  # noqa: PLR2004

  def test_clear_empties(self) -> None:
    lst: LockedList[int] = LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    lst.clear()
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
    pool, _ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()

    pool.release(handle, is_fatal=False)
    reused, _ = pool.acquire()

    assert reused is handle
    assert len(provider.opened) == 1  # no second Transport dialed -- the idle channel was reused


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
