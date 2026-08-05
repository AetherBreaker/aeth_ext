"""Tests for `aeth_ext.errors.shutdown`'s state object.

Unlike the module-level `SHUTDOWN` singleton -- which is one-shot and
process-global, so tripping it would poison the rest of the pytest session --
`ShutdownState` is an ordinary class. Every test here builds its own throwaway
instance, so the escalation and wait semantics can be exercised in-process
without a `-O` subprocess. Only tests that trip the real singleton need the
`_optimized_scenarios.py` harness.
"""

# Standard library imports
import asyncio
import threading

# Third party imports
import pytest

# First party imports
from aeth_ext.errors.shutdown import ShutdownKind, ShutdownState


class TestInitialState:
  def test_fresh_state_is_running(self):
    assert ShutdownState().kind is ShutdownKind.RUNNING

  def test_fresh_state_is_not_set(self):
    assert not ShutdownState().is_set()

  def test_wait_times_out_while_running(self):
    assert ShutdownState().wait(timeout=0.01) is False


class TestRequest:
  def test_graceful_sets_kind(self):
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.GRACEFUL

  def test_fatal_sets_kind(self):
    state = ShutdownState()
    state.request(ShutdownKind.FATAL)
    assert state.kind is ShutdownKind.FATAL

  @pytest.mark.parametrize("kind", [ShutdownKind.GRACEFUL, ShutdownKind.FATAL])
  def test_request_sets_the_event(self, kind: ShutdownKind):
    state = ShutdownState()
    state.request(kind)
    assert state.is_set()

  def test_requesting_running_is_rejected(self):
    """RUNNING is the absence of a request, not something that can be asked for."""
    state = ShutdownState()
    with pytest.raises(ValueError):
      state.request(ShutdownKind.RUNNING)
    assert not state.is_set()


class TestMonotonicEscalation:
  def test_graceful_then_fatal_escalates(self):
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    state.request(ShutdownKind.FATAL)
    assert state.kind is ShutdownKind.FATAL

  def test_fatal_then_graceful_does_not_downgrade(self):
    state = ShutdownState()
    state.request(ShutdownKind.FATAL)
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.FATAL

  def test_repeated_requests_are_idempotent(self):
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.GRACEFUL

  def test_concurrent_setters_both_succeed_and_fatal_wins(self):
    """Neither setter is lost regardless of arrival order -- there is no CAS to
    fail and no lock to serialize on, so the max simply holds."""
    for _ in range(50):
      state = ShutdownState()
      barrier = threading.Barrier(2)

      def _request(kind: ShutdownKind, state: ShutdownState = state, barrier: threading.Barrier = barrier) -> None:
        barrier.wait()
        state.request(kind)

      threads = [
        threading.Thread(target=_request, args=(ShutdownKind.GRACEFUL,)),
        threading.Thread(target=_request, args=(ShutdownKind.FATAL,)),
      ]
      for t in threads:
        t.start()
      for t in threads:
        t.join()

      assert state.kind is ShutdownKind.FATAL


class TestWaiters:
  def test_wait_returns_true_once_set(self):
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    assert state.wait(timeout=0.01) is True

  def test_wait_wakes_when_another_thread_requests(self):
    state = ShutdownState()
    threading.Timer(0.05, lambda: state.request(ShutdownKind.GRACEFUL)).start()
    assert state.wait(timeout=5.0) is True

  def test_a_woken_waiter_never_observes_running(self):
    """The kind sub-event must be set *before* the wake event, or a waiter can
    wake up and read a state that has not been published yet."""
    state = ShutdownState()
    observed: list[ShutdownKind] = []

    def _waiter() -> None:
      state.wait(timeout=5.0)
      observed.append(state.kind)

    thread = threading.Thread(target=_waiter)
    thread.start()
    state.request(ShutdownKind.FATAL)
    thread.join(timeout=5.0)

    assert observed == [ShutdownKind.FATAL]

  def test_is_awaitable(self):
    async def _run() -> ShutdownKind:
      state = ShutdownState()
      asyncio.get_running_loop().call_later(0.05, state.request, ShutdownKind.GRACEFUL)
      await state
      return state.kind

    assert asyncio.run(_run()) is ShutdownKind.GRACEFUL

  def test_works_with_asyncio_wait_for(self):
    """`heartbeat.py` waits on the event with a timeout as its sleep."""

    async def _run() -> bool:
      state = ShutdownState()
      try:
        await asyncio.wait_for(state, timeout=0.05)
      except TimeoutError:
        return True
      return False

    assert asyncio.run(_run()) is True
