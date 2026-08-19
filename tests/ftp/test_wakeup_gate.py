"""Unit tests for `aeth_ext.ftp.pool.base.WakeupGate` -- pure retry signalling, no pool involved.

Every test that parks a thread joins with a timeout and asserts the thread finished, so a regression
that reintroduces a lost or slept-through wakeup fails the suite instead of hanging it forever.
"""

# Standard library imports
import threading
from time import sleep

# Third party imports
import pytest

# First party imports
from aeth_ext.ftp.errors import PoolClosedError
from aeth_ext.ftp.pool.base import WakeupGate
from tests.ftp.conftest import wait_until

_SETTLE = 0.2
"""Seconds to let a misbehaving gate keep calling `attempt()` before asserting it didn't."""


def _never_ready() -> str | None:
  """Stands in for an acquire attempt that never finds capacity.

  Returns:
    Always `None`.
  """
  return None


class TestStaleSignalsDoNotAccumulate:
  def test_signals_with_nobody_parked_cost_exactly_one_attempt(self) -> None:
    # The crux of the epoch-counter design: signals are a "state changed" edge, not queued work. A
    # token-per-signal queue would hand the first waiter all 100 stale tokens and burn an attempt on
    # each, re-running the caller's whole (network-touching) acquire decision 100 times for nothing.
    gate = WakeupGate()
    calls = 0
    calls_lock = threading.Lock()
    result: str | None = None
    return_value: str | None = None

    def attempt() -> str | None:
      nonlocal calls
      with calls_lock:
        calls += 1
      return return_value

    def observed() -> int:
      with calls_lock:
        return calls

    def run() -> None:
      nonlocal result
      result = gate.retry_until(attempt)

    for _ in range(100):
      gate.signal()
    waiter = threading.Thread(target=run, daemon=True)
    waiter.start()

    assert wait_until(lambda: observed() >= 1)
    sleep(_SETTLE)
    assert observed() == 1

    gate.signal()
    assert wait_until(lambda: observed() >= 2)  # noqa: PLR2004
    sleep(_SETTLE)
    assert observed() == 2  # noqa: PLR2004

    return_value = "handle"
    gate.signal()
    waiter.join(timeout=5)
    assert not waiter.is_alive(), "retry_until must return as soon as attempt() produces a value"
    assert result == "handle"


class TestSignalDuringAttempt:
  def test_a_signal_landing_mid_attempt_forces_an_immediate_retry(self) -> None:
    # attempt() signals from inside itself, i.e. strictly after retry_until sampled the epoch and
    # strictly before it can park -- the exact window a bare Condition would sleep straight through.
    gate = WakeupGate()
    calls = 0
    result: str | None = None

    def attempt() -> str | None:
      nonlocal calls
      calls += 1
      if calls == 1:
        gate.signal()
        return None
      return "handle"

    def run() -> None:
      nonlocal result
      result = gate.retry_until(attempt)

    waiter = threading.Thread(target=run, daemon=True)
    waiter.start()
    waiter.join(timeout=5)

    assert not waiter.is_alive(), "an epoch bump during attempt() must retry, not park forever"
    assert result == "handle"


class TestTerminalClose:
  def test_close_releases_a_parked_waiter(self) -> None:
    gate = WakeupGate()
    parked = threading.Event()
    raised: list[Exception] = []

    def attempt() -> str | None:
      parked.set()
      return None

    def run() -> None:
      try:
        gate.retry_until(attempt)
      except Exception as exc:  # noqa: BLE001 -- deliberately broad: the assertion below is what pins the type
        raised.append(exc)

    waiter = threading.Thread(target=run, daemon=True)
    waiter.start()
    assert parked.wait(timeout=5)
    sleep(0.05)  # give the waiter time to actually reach cond.wait(); it must fail out either way

    gate.close()
    waiter.join(timeout=5)

    assert not waiter.is_alive(), "close() must release every parked waiter"
    assert len(raised) == 1
    assert isinstance(raised[0], PoolClosedError)

  def test_an_already_closed_gate_rejects_both_entry_points(self) -> None:
    gate = WakeupGate()
    gate.close()

    with pytest.raises(PoolClosedError):
      gate.raise_if_closed()
    with pytest.raises(PoolClosedError):
      gate.retry_until(_never_ready)
