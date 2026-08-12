"""Tests for `aeth_ext.errors.shutdown`'s state object and teardown behavior.

Unlike the module-level `SHUTDOWN` singleton -- which is one-shot and
process-global, so tripping it would poison the rest of the pytest session --
`ShutdownState` is an ordinary class. Every test here builds its own throwaway
instance, so the escalation and wait semantics can be exercised in-process
without a `-O` subprocess. Only tests that trip the real singleton need a
subprocess harness.

The signal ladder and the fd-2 diagnostics are exercised through
`_shutdown_signal_scenarios.py`, in fresh `-O` interpreters: the handler is a
no-op under `__debug__ == True` (see `install_shutdown_signal_handlers`), and
driving it for real trips the process-wide `SHUTDOWN`.
"""

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
from aeth_ext.errors.shutdown import ShutdownKind, ShutdownPhase, ShutdownState

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Mapping, Sequence

_SCENARIOS_SCRIPT = Path(__file__).parent / "_shutdown_signal_scenarios.py"


def _run_optimized(scenario_name: str) -> tuple[Mapping[str, object], str]:
  """Run the named scenario from `_shutdown_signal_scenarios.py` in a fresh `-O` subprocess.

  Returns the parsed JSON result *and* the subprocess's standard error, since
  the behavioral scenarios assert on what the shutdown wrote to its own fd 2 as
  well as on what it returned.

  A deliberate sibling of `test_init.py`'s identically-named helper rather than
  an import of it: that one owns the *registration* scenarios in the same script
  and needs only the JSON, and `test_err_handling.py` already establishes the
  convention that each test module carries its own runner for the scenario
  script it drives.
  """
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


_ELAPSED_PREFIX = re.compile(r"^\[shutdown \+\d+\.\d{2}s\] ")
"""The prefix `_emit` renders once `_t0` is set."""

_BARE_PREFIX = "[shutdown] "
"""The prefix `_emit` renders while `_t0` is still `None`."""


def _shutdown_lines(stderr: str) -> list[str]:
  """The subprocess's `_emit` lines, in order, dropping everything else.

  Filtering matters: the one sanctioned `logger.critical` marker goes through
  logging, whose last-resort handler also writes to fd 2 in a subprocess with no
  configuration, so raw stderr carries a line these assertions must not count.
  """
  return [line for line in stderr.splitlines() if line.startswith("[shutdown")]


def _messages(stderr: str) -> list[str]:
  """`_shutdown_lines`, with each line's prefix stripped off."""
  return [line.partition("] ")[2] for line in _shutdown_lines(stderr)]


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

  def test_forced_sets_kind(self):
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    assert state.kind is ShutdownKind.FORCED

  @pytest.mark.parametrize("kind", [ShutdownKind.GRACEFUL, ShutdownKind.FATAL, ShutdownKind.FORCED])
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

  def test_graceful_then_forced_escalates(self):
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)
    state.request(ShutdownKind.FORCED)
    assert state.kind is ShutdownKind.FORCED

  def test_forced_then_graceful_does_not_downgrade(self):
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    state.request(ShutdownKind.GRACEFUL)
    assert state.kind is ShutdownKind.FORCED

  def test_fatal_then_forced_escalates(self):
    state = ShutdownState()
    state.request(ShutdownKind.FATAL)
    state.request(ShutdownKind.FORCED)
    assert state.kind is ShutdownKind.FORCED

  def test_forced_then_fatal_does_not_downgrade(self):
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
    state.request(ShutdownKind.FATAL)
    assert state.kind is ShutdownKind.FORCED

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

  def test_wait_returns_true_after_forced_request(self):
    state = ShutdownState()
    state.request(ShutdownKind.FORCED)
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


class TestBudgets:
  def test_forced_budget_is_zero(self):
    """A zero budget is what makes FORCED drop every non-required callback
    from the threaded pass's first iteration onward -- see the `>=` comparison
    in `_run_threaded_pass`."""
    assert shutdown_module._BUDGETS[ShutdownKind.FORCED] == 0.0  # pyright: ignore[reportPrivateUsage]


def _drive_threaded_pass(
  monkeypatch: pytest.MonkeyPatch,
  *,
  state: ShutdownState,
  registrations: Sequence[tuple[Callable[[], None], bool]],
) -> list[str]:
  """Run `_run_threaded_pass` in-process against *state* and *registrations*.

  Deliberately **not** a `-O` subprocess, even though the pass is normally
  reached only through the process-global `SHUTDOWN`. The budget rule under test
  is a comparison against a clock, and the `>=` it uses is only distinguishable
  from a `>` when the elapsed time is *exactly* the budget. A real subprocess
  can never produce that: `time.monotonic()` is sub-microsecond on every
  platform this runs on, so any elapsed figure it yields is strictly greater
  than a zero budget and both comparisons would agree. Freezing the clock is the
  only way to pin elapsed to 0.0, and freezing it means patching the module
  anyway -- at which point running in-process costs nothing extra and reports
  failures directly instead of through a JSON round trip.

  Nothing here touches the real `SHUTDOWN`: *state* is a throwaway instance
  bound over the module's name for the duration of the test, and `monkeypatch`
  restores every global on teardown.

  Returns the text passed to each `_emit` call, in order.
  """
  monkeypatch.setattr(shutdown_module, "SHUTDOWN", state)
  # Elapsed pinned to exactly 0.0 -- see this function's docstring.
  monkeypatch.setattr(shutdown_module, "monotonic", lambda: 0.0)
  monkeypatch.setattr(shutdown_module, "_t0", 0.0)

  emitted: list[str] = []
  monkeypatch.setattr(shutdown_module, "_emit", emitted.append)
  # The pass ends by nudging the main thread out of the interpreter. That is
  # covered by the `-O` scenarios below, and must never happen to pytest itself.
  monkeypatch.setattr(shutdown_module, "_attempt_early_exit", lambda: None)
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
  """A zero budget drops every non-`required` threaded callback, from the first one on."""

  def test_forced_skips_non_required_callbacks_including_the_first(self, monkeypatch: pytest.MonkeyPatch):
    """The `>=` fix: with elapsed pinned to exactly the (zero) budget, the very
    first non-required callback is skipped. Under the `>` this replaced, it
    would have run and only its successors would have been dropped."""
    ran: list[str] = []

    def first_optional() -> None:
      ran.append("first_optional")

    def the_required_one() -> None:
      ran.append("the_required_one")

    def second_optional() -> None:
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

  def test_graceful_runs_the_same_registry_in_full(self, monkeypatch: pytest.MonkeyPatch):
    """The control for the test above: nothing about that registry is inherently
    skippable, so the skipping there is the budget's doing and not the wiring's."""
    ran: list[str] = []

    def first_optional() -> None:
      ran.append("first_optional")

    def the_required_one() -> None:
      ran.append("the_required_one")

    def second_optional() -> None:
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

  def test_escalating_to_forced_mid_pass_skips_the_rest(self, monkeypatch: pytest.MonkeyPatch):
    """The kind is re-read every iteration, so an escalation arriving while the
    pass is running shrinks the budget for everything still to come."""
    ran: list[str] = []
    state = ShutdownState()
    state.request(ShutdownKind.GRACEFUL)

    def escalates_to_forced() -> None:
      ran.append("escalates_to_forced")
      state.request(ShutdownKind.FORCED)

    def optional_after_the_escalation() -> None:
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


class TestSignalLadder:
  """D-I3: four SIGINTs, one rung each -- start, warn, force, hard interrupt."""

  def test_each_press_climbs_exactly_one_rung(self):
    result, _stderr = _run_optimized("signal_ladder_climbs_all_four_rungs")

    # First press starts a graceful shutdown; the second only warns and must
    # escalate nothing; the third forces; the fourth changes no kind at all.
    assert result["kinds"] == ["GRACEFUL", "GRACEFUL", "FORCED", "FORCED"]
    assert result["hard_interrupted"] is True

  def test_each_rung_announces_itself_once_and_in_order(self):
    _result, stderr = _run_optimized("signal_ladder_climbs_all_four_rungs")

    rungs = [text for text in _messages(stderr) if text in {_RUNG_1, _RUNG_2, _RUNG_3, _RUNG_4}]

    assert rungs == [_RUNG_1, _RUNG_2, _RUNG_3, _RUNG_4]


class TestExitNudgeShortCircuit:
  """D-I4: our own `interrupt_main()` re-enters this handler and must not land on a rung."""

  def test_nudge_goes_straight_to_the_stock_handler(self):
    result, stderr = _run_optimized("exit_nudge_short_circuits_past_the_ladder")

    assert result["hard_interrupted"] is True
    # Rung 1 would have started one; it is checked after the nudge flag, so it
    # never ran.
    assert result["shutdown_kind"] == "RUNNING"
    # The first ticket is still on offer, so no rung of the `match` was entered.
    assert result["confirm_ticket"] == 0
    assert _shutdown_lines(stderr) == []


class TestShutdownOutput:
  """What a real, small graceful shutdown writes to fd 2."""

  def test_lines_are_the_two_banners_bracketing_the_rung_that_started_them(self):
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    assert _messages(stderr) == [
      _RUNG_1,
      "GRACEFUL requested; 2 threaded callbacks",
      "teardown complete; 2 run, 0 skipped",
    ]

  def test_the_rung_that_precedes_run_shutdown_has_no_elapsed_figure(self):
    """`_t0` is set by the first driver inside `run_shutdown`, and rung 1 speaks
    before it decides to call it -- the one line that can carry a bare prefix."""
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    assert _shutdown_lines(stderr)[0] == f"{_BARE_PREFIX}{_RUNG_1}"

  def test_every_later_line_carries_an_elapsed_figure(self):
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    later = _shutdown_lines(stderr)[1:]

    assert later
    assert all(_ELAPSED_PREFIX.match(line) for line in later)

  def test_the_logging_marker_is_not_one_of_these_lines(self):
    """The single sanctioned `logger.critical` goes through logging, not `_emit`
    -- it shares fd 2 here only because the subprocess has no handler configured."""
    _result, stderr = _run_optimized("shutdown_output_is_written_to_fd_2")

    assert "Process shutdown underway: GRACEFUL" in stderr
    assert not any("Process shutdown underway" in line for line in _shutdown_lines(stderr))
