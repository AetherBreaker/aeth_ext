# Standard library imports
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from threading import Event as ThreadingEvent
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.errors import shutdown as shutdown_module
from aeth_ext.errors.exception_trail import build_exception_trail
from aeth_ext.errors.shutdown import ShutdownCompletion, ShutdownKind, ShutdownPhase, ShutdownState

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Mapping, Sequence

  # First party imports
  from aeth_ext.errors.exception_trail import ExceptionTrail

_SCENARIOS_SCRIPT = Path(__file__).parent / "_shutdown_signal_scenarios.py"


def _run_optimized(scenario_name: str) -> tuple[Mapping[str, object], str]:
  env = dict(os.environ)
  # aeth_ext's settings require this at import time; the scenario never sends mail.
  env.setdefault("ALERTS_EMAIL_PWD", "test-password")

  proc = subprocess.run(
    [sys.executable, "-O", str(_SCENARIOS_SCRIPT), scenario_name],
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
    check=False,
  )

  assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

  return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


_TRAIL_SCENARIOS_SCRIPT = Path(__file__).parent / "_exception_trail_shutdown_scenarios.py"


def _run_trail_scenario(scenario_name: str) -> Mapping[str, object]:
  env = dict(os.environ)
  env.setdefault("ALERTS_EMAIL_PWD", "test-password")

  proc = subprocess.run(
    [sys.executable, "-O", str(_TRAIL_SCENARIOS_SCRIPT), scenario_name],
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
    check=False,
  )

  assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

  return json.loads(proc.stdout.strip().splitlines()[-1])


_ELAPSED_PREFIX = re.compile(r"^\[shutdown \+\d+\.\d{2}s\] ")
"""The prefix `_emit` renders once `_t0` is set."""

_BARE_PREFIX = "[shutdown] "
"""The prefix `_emit` renders while `_t0` is still `None`."""


def _shutdown_lines(stderr: str) -> list[str]:
  return [line for line in stderr.splitlines() if line.startswith("[shutdown")]


def _messages(stderr: str) -> list[str]:
  return [line.partition("] ")[2] for line in _shutdown_lines(stderr)]


class TestInitialState:
  def test_fresh_state_is_running(self) -> None:
    assert ShutdownState().kind is ShutdownKind.RUNNING

  def test_fresh_state_is_not_set(self) -> None:
    assert not ShutdownState().is_set()

  def test_wait_times_out_while_running(self) -> None:
    assert ShutdownState().wait(timeout=0.01) is False


class TestRequest:
  def test_graceful_sets_kind(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.GRACEFUL

  def test_fatal_sets_kind(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.FATAL)
    assert state.kind is ShutdownKind.FATAL

  def test_forced_sets_kind(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    assert state.kind is ShutdownKind.FORCED

  @pytest.mark.parametrize("kind", [ShutdownKind.GRACEFUL, ShutdownKind.FATAL, ShutdownKind.FORCED])
  def test_request_sets_the_event(self, kind: ShutdownKind) -> None:
    state = ShutdownState()
    state.request(kind)
    assert state.is_set()

  def test_requesting_running_is_rejected(self) -> None:
    state = ShutdownState()
    with pytest.raises(ValueError):
      state.request(ShutdownKind.RUNNING)
    assert not state.is_set()


class TestMonotonicEscalation:
  def test_graceful_then_fatal_escalates(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    state.request(ShutdownKind.FATAL)
    assert state.kind is ShutdownKind.FATAL

  def test_fatal_then_graceful_does_not_downgrade(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.FATAL)
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.FATAL

  def test_graceful_then_forced_escalates(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    state.request(ShutdownKind.FORCED)
    assert state.kind is ShutdownKind.FORCED

  def test_forced_then_graceful_does_not_downgrade(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.FORCED

  def test_fatal_then_forced_escalates(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.FATAL)
    state.request(ShutdownKind.FORCED)
    assert state.kind is ShutdownKind.FORCED

  def test_forced_then_fatal_does_not_downgrade(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    state.request(ShutdownKind.FATAL)
    assert state.kind is ShutdownKind.FORCED

  def test_repeated_requests_are_idempotent(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.GRACEFUL

  def test_concurrent_setters_both_succeed_and_fatal_wins(self) -> None:
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
  def test_wait_returns_true_once_set(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    assert state.wait(timeout=0.01) is True

  def test_wait_returns_true_after_forced_request(self) -> None:
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    assert state.wait(timeout=0.01) is True

  def test_wait_wakes_when_another_thread_requests(self) -> None:
    state = ShutdownState()
    threading.Timer(0.05, lambda: state.request(ShutdownKind.GRACEFUL)).start()
    assert state.wait(timeout=5.0) is True

  def test_a_woken_waiter_never_observes_running(self) -> None:
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

  def test_is_awaitable(self) -> None:
    async def _run() -> ShutdownKind:
      state = ShutdownState()
      asyncio.get_running_loop().call_later(0.05, state.request, ShutdownKind.GRACEFUL)
      await state
      return state.kind

    assert asyncio.run(_run()) is ShutdownKind.GRACEFUL

  def test_works_with_asyncio_wait_for(self) -> None:
    async def _run() -> bool:
      state = ShutdownState()
      try:
        await asyncio.wait_for(state, timeout=0.05)
      except TimeoutError:
        return True
      return False

    assert asyncio.run(_run()) is True


class TestBudgets:
  def test_forced_budget_is_zero(self) -> None:
    assert shutdown_module._BUDGETS[ShutdownKind.FORCED] == 0.0  # pyright: ignore[reportPrivateUsage]


def _drive_threaded_pass(
  monkeypatch: pytest.MonkeyPatch,
  *,
  state: ShutdownState,
  registrations: Sequence[tuple[Callable[[tuple[ExceptionTrail, ...]], None], bool]],
  freeze_clock: bool = True,
) -> list[str]:
  monkeypatch.setattr(shutdown_module, "SHUTDOWN", state)
  if freeze_clock:
    # Elapsed pinned to exactly 0.0 -- see this function's docstring.
    monkeypatch.setattr(shutdown_module, "monotonic", lambda: 0.0)
    monkeypatch.setattr(shutdown_module, "_t0", 0.0)
  else:
    monkeypatch.setattr(shutdown_module, "_t0", shutdown_module.monotonic())

  emitted: list[str] = []
  monkeypatch.setattr(shutdown_module, "_emit", emitted.append)
  # The pass ends by nudging the main thread out of the interpreter. That is
  # covered by the `-O` scenarios below, and must never happen to pytest itself.
  monkeypatch.setattr(shutdown_module, "_attempt_early_exit", lambda: None)
  # The pass also sets the process-wide SHUTDOWN_COMPLETE, which is one-shot
  # like SHUTDOWN itself and must never be tripped by an in-process test.
  monkeypatch.setattr(shutdown_module, "SHUTDOWN_COMPLETE", ShutdownCompletion())
  # Pre-set, so the pass's wait for `run_shutdown` to return is a no-op here --
  # there is no `run_shutdown` call in this arrangement to wait for.
  released = ThreadingEvent()
  released.set()
  monkeypatch.setattr(shutdown_module, "_drive_released", released)
  # The single sanctioned `logger.critical` marker is not what these tests
  # assert on, and letting it reach logging's last-resort handler would put an
  # unrelated CRITICAL line in the suite's output.
  monkeypatch.setattr(shutdown_module.logger, "critical", lambda *args, **kwargs: None)

  # Registered through the real entry point, against an empty registry, so the
  # labels, ordering and weak-getter behavior under test are the genuine ones.
  monkeypatch.setattr(shutdown_module, "_registrations", ())
  for callback, required in registrations:
    shutdown_module.register_for_shutdown(callback, phase=ShutdownPhase.THREADED, required=required)

  shutdown_module._run_threaded_pass([])  # pyright: ignore[reportPrivateUsage]
  return emitted


def _skipped_labels(emitted: Sequence[str]) -> list[str]:
  prefix = "WARN budget exhausted; skipping "
  return [line.removeprefix(prefix) for line in emitted if line.startswith(prefix)]


class TestForcedBudgetSkipping:
  def test_forced_skips_non_required_callbacks_including_the_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def first_optional(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("first_optional")

    def the_required_one(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("the_required_one")

    def second_optional(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("second_optional")

    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    emitted = _drive_threaded_pass(
      monkeypatch,
      state=state,
      registrations=[(first_optional, False), (the_required_one, True), (second_optional, False)],
    )

    assert ran == ["the_required_one"]
    assert [label.rpartition(".")[2] for label in _skipped_labels(emitted)] == ["first_optional", "second_optional"]
    assert emitted[0] == "FORCED requested; 3 threaded callbacks"
    assert emitted[-1] == "teardown complete; 1 run, 2 skipped"

  def test_graceful_runs_the_same_registry_in_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def first_optional(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("first_optional")

    def the_required_one(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("the_required_one")

    def second_optional(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("second_optional")

    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    emitted = _drive_threaded_pass(
      monkeypatch,
      state=state,
      registrations=[(first_optional, False), (the_required_one, True), (second_optional, False)],
    )

    assert ran == ["first_optional", "the_required_one", "second_optional"]
    assert _skipped_labels(emitted) == []
    assert emitted[0] == "GRACEFUL requested; 3 threaded callbacks"
    assert emitted[-1] == "teardown complete; 3 run, 0 skipped"

  def test_escalating_to_forced_mid_pass_skips_the_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)

    def escalates_to_forced(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("escalates_to_forced")
      state.request(ShutdownKind.FORCED)

    def optional_after_the_escalation(trails: tuple[ExceptionTrail, ...]) -> None:
      ran.append("optional_after_the_escalation")

    emitted = _drive_threaded_pass(
      monkeypatch,
      state=state,
      registrations=[(escalates_to_forced, True), (optional_after_the_escalation, False)],
    )

    assert ran == ["escalates_to_forced"]
    assert [label.rpartition(".")[2] for label in _skipped_labels(emitted)] == ["optional_after_the_escalation"]
    # The banner reads the kind when it is written, which is before the
    # escalation happens -- the start and end banners bracket a pass whose kind
    # changed underneath them.
    assert emitted[0] == "GRACEFUL requested; 2 threaded callbacks"
    assert emitted[-1] == "teardown complete; 1 run, 1 skipped"


_RUNG_1 = "shutdown underway; interrupt again to force"
_RUNG_2 = "shutdown ALREADY underway; interrupt again to FORCE (drops non-critical teardown)"
_RUNG_3 = "FORCING; dropping non-critical teardown"
_RUNG_4 = "hard interrupt"


class TestShutdownCompletion:
  def test_fresh_instance_is_not_set(self) -> None:
    assert not ShutdownCompletion().is_set()

  def test_wait_times_out_while_pending(self) -> None:
    assert ShutdownCompletion().wait(timeout=0.01) is False

  def test_wait_returns_true_once_done(self) -> None:
    done = ShutdownCompletion()
    done._done.set()  # pyright: ignore[reportPrivateUsage]
    assert done.wait(timeout=0) is True
    assert done.is_set()

  def test_is_awaitable(self) -> None:
    done = ShutdownCompletion()

    async def wake_then_await() -> bool:
      asyncio.get_running_loop().call_soon(done._done.set)  # pyright: ignore[reportPrivateUsage]
      return await done

    assert asyncio.run(wake_then_await()) is True

  @pytest.mark.parametrize("via", ["wait", "await"])
  def test_waiting_declares_a_tail(self, via: str) -> None:
    done = ShutdownCompletion()
    assert not done._waited.is_set()  # pyright: ignore[reportPrivateUsage]

    if via == "wait":
      done.wait(timeout=0)
    else:

      async def touch() -> None:
        done._done.set()  # pyright: ignore[reportPrivateUsage]
        await done

      asyncio.run(touch())

    assert done._waited.is_set()  # pyright: ignore[reportPrivateUsage]

  def test_pass_sets_it_after_the_last_callback_and_before_the_nudge(self, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    completion = ShutdownCompletion()

    def teardown(trails: tuple[ExceptionTrail, ...]) -> None:
      order.append(f"callback done={completion.is_set()}")

    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    _drive_threaded_pass(monkeypatch, state=state, registrations=[(teardown, True)])
    # The helper ran the pass once to install its patches; re-run against this
    # test's own completion instance and recording nudge to observe ordering.
    order.clear()
    monkeypatch.setattr(shutdown_module, "SHUTDOWN_COMPLETE", completion)
    monkeypatch.setattr(shutdown_module, "_attempt_early_exit", lambda: order.append(f"nudge done={completion.is_set()}"))
    shutdown_module._run_threaded_pass([])  # pyright: ignore[reportPrivateUsage]

    assert order == ["callback done=False", "nudge done=True"]


class _FakeThread:
  def __init__(self, alive: bool) -> None:
    self._alive = alive

  def is_alive(self) -> bool:
    return self._alive


class TestExitNudgeDeferral:
  @staticmethod
  def _arrange(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: ShutdownKind,
    elapsed: float,
    waited: bool,
    main_alive: bool = True,
  ) -> tuple[list[float], list[str]]:
    now = [elapsed]
    sleeps: list[float] = []
    nudges: list[str] = []

    def fake_sleep(secs: float) -> None:
      sleeps.append(secs)
      now[0] += secs

    state = ShutdownState()
    state.request(kind)
    completion = ShutdownCompletion()
    if waited:
      completion.wait(timeout=0)

    monkeypatch.setattr(shutdown_module, "SHUTDOWN", state)
    monkeypatch.setattr(shutdown_module, "SHUTDOWN_COMPLETE", completion)
    monkeypatch.setattr(shutdown_module, "_t0", 0.0)
    monkeypatch.setattr(shutdown_module, "monotonic", lambda: now[0])
    monkeypatch.setattr(shutdown_module, "sleep", fake_sleep)
    monkeypatch.setattr(shutdown_module, "main_thread", lambda: _FakeThread(main_alive))
    monkeypatch.setattr(shutdown_module, "interrupt_main", lambda: nudges.append("nudge"))
    monkeypatch.setattr(shutdown_module, "_exit_nudge_sent", False)
    return sleeps, nudges

  def test_no_waiter_nudges_after_the_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
    grace = shutdown_module._NUDGE_GRACE_SECS  # pyright: ignore[reportPrivateUsage]
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.GRACEFUL, elapsed=0.0, waited=False)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    assert sum(sleeps) == pytest.approx(grace)
    assert nudges == ["nudge"]
    assert shutdown_module._exit_nudge_sent is True  # pyright: ignore[reportPrivateUsage]

  def test_no_waiter_under_forced_nudges_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.FORCED, elapsed=0.0, waited=False)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    assert sleeps == []
    assert nudges == ["nudge"]

  def test_waiter_arriving_during_the_grace_extends_to_the_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
    budget = shutdown_module._GRACEFUL_BUDGET_SECS  # pyright: ignore[reportPrivateUsage]
    margin = shutdown_module._NUDGE_MARGIN_SECS  # pyright: ignore[reportPrivateUsage]
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.GRACEFUL, elapsed=0.0, waited=False)
    advance_clock = shutdown_module.sleep

    def wait_on_first_tick(secs: float) -> None:
      advance_clock(secs)
      if len(sleeps) == 1:
        shutdown_module.SHUTDOWN_COMPLETE.wait(timeout=0)

    monkeypatch.setattr(shutdown_module, "sleep", wait_on_first_tick)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    assert sum(sleeps) == pytest.approx(budget - margin)
    assert nudges == ["nudge"]

  def test_waiter_defers_for_the_rest_of_the_budget_minus_the_margin(self, monkeypatch: pytest.MonkeyPatch) -> None:
    budget = shutdown_module._GRACEFUL_BUDGET_SECS  # pyright: ignore[reportPrivateUsage]
    margin = shutdown_module._NUDGE_MARGIN_SECS  # pyright: ignore[reportPrivateUsage]
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.GRACEFUL, elapsed=1.0, waited=True)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    assert sum(sleeps) == pytest.approx(budget - margin - 1.0)
    assert nudges == ["nudge"]

  def test_budget_already_exhausted_still_gives_the_tail_the_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
    budget = shutdown_module._GRACEFUL_BUDGET_SECS  # pyright: ignore[reportPrivateUsage]
    grace = shutdown_module._NUDGE_GRACE_SECS  # pyright: ignore[reportPrivateUsage]
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.GRACEFUL, elapsed=budget + 3.0, waited=True)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    assert sum(sleeps) == pytest.approx(grace)
    assert nudges == ["nudge"]

  def test_forced_gives_the_tail_only_the_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
    grace = shutdown_module._NUDGE_GRACE_SECS  # pyright: ignore[reportPrivateUsage]
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.FORCED, elapsed=0.0, waited=True)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    assert sum(sleeps) == pytest.approx(grace)
    assert nudges == ["nudge"]

  def test_escalation_mid_deferral_cuts_it_short(self, monkeypatch: pytest.MonkeyPatch) -> None:
    escalate_after_ticks = 2
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.GRACEFUL, elapsed=0.0, waited=True)
    advance_clock = shutdown_module.sleep

    def escalate_on_second_tick(secs: float) -> None:
      advance_clock(secs)
      if len(sleeps) == escalate_after_ticks:
        shutdown_module.SHUTDOWN.request(ShutdownKind.FORCED)

    monkeypatch.setattr(shutdown_module, "sleep", escalate_on_second_tick)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    # Cut to the grace floor, not to zero: 0.2s had elapsed when FORCED landed.
    grace = shutdown_module._NUDGE_GRACE_SECS  # pyright: ignore[reportPrivateUsage]
    assert sum(sleeps) == pytest.approx(grace)
    assert nudges == ["nudge"]

  def test_main_thread_already_finished_skips_the_nudge(self, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps, nudges = self._arrange(monkeypatch, kind=ShutdownKind.GRACEFUL, elapsed=0.0, waited=True, main_alive=False)
    shutdown_module._attempt_early_exit()  # pyright: ignore[reportPrivateUsage]
    assert sleeps == []
    assert nudges == []
    assert shutdown_module._exit_nudge_sent is False  # pyright: ignore[reportPrivateUsage]


class TestOptionalCallbacksAreBounded:
  def test_required_runs_inline_and_optional_on_a_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
    threads: dict[str, str] = {}

    def required(trails: tuple[ExceptionTrail, ...]) -> None:
      threads["required"] = threading.current_thread().name

    def optional(trails: tuple[ExceptionTrail, ...]) -> None:
      threads["optional"] = threading.current_thread().name

    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    _drive_threaded_pass(monkeypatch, state=state, registrations=[(required, True), (optional, False)])

    assert threads["required"] == threading.current_thread().name
    assert threads["optional"].startswith("aeth-ext-teardown:")

  def test_an_optional_callback_that_overruns_is_abandoned(self, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = ThreadingEvent()
    after: list[str] = []

    def hangs(trails: tuple[ExceptionTrail, ...]) -> None:
      gate.wait(timeout=15.0)

    def next_one(trails: tuple[ExceptionTrail, ...]) -> None:
      after.append("ran")

    # A tiny budget so the abandonment is reached in real time, and a real
    # clock so the join loop actually advances.
    monkeypatch.setitem(shutdown_module._BUDGETS, ShutdownKind.GRACEFUL, 0.3)  # pyright: ignore[reportPrivateUsage]
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    try:
      emitted = _drive_threaded_pass(monkeypatch, state=state, registrations=[(hangs, False), (next_one, True)], freeze_clock=False)
    finally:
      gate.set()

    # Labels are qualnames, so the nested function's carries a `<locals>` prefix.
    assert any(text.startswith("WARN budget exhausted; abandoning") and text.endswith(".hangs") for text in emitted)
    assert after == ["ran"]
    assert emitted[-1] == "teardown complete; 2 run, 0 skipped"

  def test_a_failing_optional_callback_is_reported_not_propagated(self, monkeypatch: pytest.MonkeyPatch) -> None:
    def explodes(trails: tuple[ExceptionTrail, ...]) -> None:
      raise RuntimeError("boom")

    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    emitted = _drive_threaded_pass(monkeypatch, state=state, registrations=[(explodes, False)])

    assert any(text.startswith("TEARDOWN FAILED:") and ".explodes\n" in text and "boom" in text for text in emitted)
    assert emitted[-1] == "teardown complete; 1 run, 0 skipped"

  def test_hung_optional_callback_does_not_hold_process_exit(self) -> None:
    result, _stderr = _run_optimized("hung_optional_callback_does_not_hold_exit")
    assert result["exited_within_budget"] is True


class TestJoinPassAtExit:
  def test_is_a_no_op_before_any_pass(self) -> None:
    shutdown_module._join_pass_at_exit()  # pyright: ignore[reportPrivateUsage]  -- _pass_thread is None

  def test_run_shutdown_registers_it_after_starting_the_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    state = ShutdownState()
    monkeypatch.setattr(shutdown_module, "SHUTDOWN", state)
    monkeypatch.setattr(shutdown_module, "_drive_counter", iter([0]))
    monkeypatch.setattr(shutdown_module, "_run_interrupt_pass", list)
    monkeypatch.setattr(shutdown_module, "_drive_released", ThreadingEvent())
    monkeypatch.setattr(shutdown_module, "_pass_thread", None)

    class FakeThread:
      def __init__(self, **kwargs: object) -> None:
        pass

      def start(self) -> None:
        events.append("start")

    monkeypatch.setattr(shutdown_module, "Thread", FakeThread)
    monkeypatch.setattr(shutdown_module.atexit, "register", lambda fn: events.append(f"register:{fn.__name__}"))

    shutdown_module.run_shutdown(ShutdownKind.GRACEFUL)

    assert events == ["start", "register:_join_pass_at_exit"]

  def test_joins_the_pass_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
    joined: list[str] = []

    class FakeThread:
      def join(self) -> None:
        joined.append("joined")

    monkeypatch.setattr(shutdown_module, "_pass_thread", FakeThread())
    monkeypatch.setattr(shutdown_module, "_hard_interrupted", False)
    shutdown_module._join_pass_at_exit()  # pyright: ignore[reportPrivateUsage]
    assert joined == ["joined"]

  def test_stands_down_after_a_hard_interrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
    joined: list[str] = []

    class FakeThread:
      def join(self) -> None:
        joined.append("joined")

    monkeypatch.setattr(shutdown_module, "_pass_thread", FakeThread())
    monkeypatch.setattr(shutdown_module, "_hard_interrupted", True)
    shutdown_module._join_pass_at_exit()  # pyright: ignore[reportPrivateUsage]
    assert joined == []

  def test_fourth_interrupt_escapes_a_wedged_required_callback(self) -> None:
    result, _stderr = _run_optimized("hard_interrupt_escapes_a_wedged_required_callback")
    assert result["hard_interrupted"] is True
    assert result["exited"] is True


class TestRequiredCallbackSurvivesMainReturning:
  def test_required_callback_completes_when_main_returns_on_shutdown(self) -> None:
    result, _stderr = _run_optimized("required_callback_completes_when_main_returns_on_shutdown")
    assert result["flush_ran"] is True

  def test_tail_after_shutdown_complete_runs_uninterrupted_and_exits_promptly(self) -> None:
    result, _stderr = _run_optimized("tail_after_shutdown_complete_runs_uninterrupted")
    assert result["flush_ran"] is True
    assert result["tail_ran"] is True
    assert result["tail_interrupted"] is False


class TestSignalLadder:
  def test_each_press_climbs_exactly_one_rung(self) -> None:
    result, _stderr = _run_optimized("signal_ladder_climbs_all_four_rungs")

    # First press starts a graceful shutdown; the second only warns and must
    # escalate nothing; the third forces; the fourth changes no kind at all.
    assert result["kinds"] == ["GRACEFUL", "GRACEFUL", "FORCED", "FORCED"]
    assert result["hard_interrupted"] is True

  def test_each_rung_announces_itself_once_and_in_order(self) -> None:
    _result, stderr = _run_optimized("signal_ladder_climbs_all_four_rungs")

    rungs = [text for text in _messages(stderr) if text in {_RUNG_1, _RUNG_2, _RUNG_3, _RUNG_4}]

    assert rungs == [_RUNG_1, _RUNG_2, _RUNG_3, _RUNG_4]


class TestExitNudgeShortCircuit:
  def test_nudge_goes_straight_to_the_stock_handler(self) -> None:
    result, stderr = _run_optimized("exit_nudge_short_circuits_past_the_ladder")

    assert result["hard_interrupted"] is True
    # Rung 1 would have started one; it is checked after the nudge flag, so it
    # never ran.
    assert result["shutdown_kind"] == "RUNNING"
    # The first ticket is still on offer, so no rung of the `match` was entered.
    assert result["confirm_ticket"] == 0
    assert _shutdown_lines(stderr) == []


class TestShutdownOutput:
  def test_lines_are_the_two_banners_bracketing_the_rung_that_started_them(self) -> None:
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    assert _messages(stderr) == [
      _RUNG_1,
      "GRACEFUL requested; 2 threaded callbacks",
      "teardown complete; 2 run, 0 skipped",
    ]

  def test_the_rung_that_precedes_run_shutdown_has_no_elapsed_figure(self) -> None:
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    assert _shutdown_lines(stderr)[0] == f"{_BARE_PREFIX}{_RUNG_1}"

  def test_every_later_line_carries_an_elapsed_figure(self) -> None:
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    later = _shutdown_lines(stderr)[1:]

    assert later
    assert all(_ELAPSED_PREFIX.match(line) for line in later)

  def test_the_logging_marker_is_not_one_of_these_lines(self) -> None:
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    assert "Process shutdown underway: GRACEFUL" in stderr
    assert not any("Process shutdown underway" in line for line in _shutdown_lines(stderr))


class TestGetCurrentFatalTrail:
  def test_empty_before_any_shutdown(self) -> None:
    result = _run_trail_scenario("get_current_fatal_trails_is_empty_before_any_shutdown")
    assert result == {"trail_count": 0}

  def test_returns_the_trail_set_before_a_fatal_shutdown(self) -> None:
    result = _run_trail_scenario("get_current_fatal_trails_returns_the_set_trail_after_fatal_shutdown")
    # Run as a script (`python -O script.py`), so the raising frame's own module is "__main__".
    assert result == {"trail_count": 1, "same_object": True, "origin_module": "__main__"}


class TestRegisterForShutdownTrailPassing:
  def test_callback_receives_an_empty_tuple_when_no_trail_is_set(self) -> None:
    result = _run_trail_scenario("callback_receives_an_empty_tuple_when_no_trail_is_set")
    assert result == {"received_empty_tuple": True}

  def test_callback_receives_the_trail_tuple_when_fatal(self) -> None:
    result = _run_trail_scenario("callback_receives_the_trail_tuple_when_fatal")
    assert result == {"received_one_trail": True}

  def test_interrupt_phase_callback_receives_an_empty_tuple_when_no_trail_is_set(self) -> None:
    result = _run_trail_scenario("interrupt_callback_receives_an_empty_tuple_when_no_trail_is_set")
    assert result == {"received_empty_tuple": True}

  def test_interrupt_phase_callback_receives_the_trail_tuple_when_fatal(self) -> None:
    result = _run_trail_scenario("interrupt_callback_receives_the_trail_tuple_when_fatal")
    assert result == {"received_one_trail": True}

  def test_a_second_fatal_trail_is_accumulated_not_overwritten(self) -> None:
    result = _run_trail_scenario("a_second_fatal_trail_is_accumulated_not_overwritten")
    assert result == {"trail_count": 2}


class TestConcurrentFatalTrailWrites:
  def test_two_concurrent_writes_both_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
    writer_count = 2

    def _write(label: str, barrier: threading.Barrier) -> None:
      barrier.wait(timeout=5.0)
      try:
        raise ValueError(label)
      except ValueError as e:
        shutdown_module._set_current_fatal_trail(build_exception_trail(e))  # pyright: ignore[reportPrivateUsage]

    # Repeated, like test_concurrent_setters_both_succeed_and_fatal_wins above -- a lost update
    # from an unlocked race is not guaranteed to reproduce on every run.
    for _ in range(50):
      monkeypatch.setattr(shutdown_module, "_current_fatal_trails", ())
      barrier = threading.Barrier(writer_count)
      threads = [threading.Thread(target=_write, args=(label, barrier)) for label in ("a", "b")]
      for t in threads:
        t.start()
      for t in threads:
        t.join(timeout=5.0)

      assert len(shutdown_module.get_current_fatal_trails()) == writer_count
