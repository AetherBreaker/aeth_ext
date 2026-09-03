# Standard library imports
import asyncio
import atexit
import json
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.errors import shutdown as shutdown_module
from aeth_ext.errors.shutdown import SHUTDOWN, SHUTDOWN_COMPLETE, ShutdownPhase

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # First party imports
  from aeth_ext.errors.exception_trail import ExceptionTrail

assert not __debug__, "this harness must run under python -O"

_SHUTDOWN_THREAD_NAME = "aeth-ext-shutdown"
"""The name `run_shutdown` gives the thread it hands the threaded pass to."""


def _patch_signal_signal() -> tuple[list[int], list[object]]:
  registered_signums: list[int] = []
  registered_handlers: list[object] = []

  def fake_signal(sig: int, handler: object) -> None:
    registered_signums.append(sig)
    registered_handlers.append(handler)

  signal.signal = fake_signal
  return registered_signums, registered_handlers


def sigint_is_always_registered() -> dict[str, object]:
  registered_signums, _ = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {"sigint_registered": signal.SIGINT in registered_signums}


def windows_registers_sigbreak_when_available_and_not_sigterm() -> dict[str, object]:
  registered_signums, _ = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {
    "has_sigbreak": hasattr(signal, "SIGBREAK"),
    "sigbreak_registered": getattr(signal, "SIGBREAK", None) in registered_signums,
    "sigterm_registered": signal.SIGTERM in registered_signums,
  }


def posix_registers_sigterm() -> dict[str, object]:
  registered_signums, _ = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {"sigterm_registered": signal.SIGTERM in registered_signums}


def registers_the_module_level_handler() -> dict[str, object]:
  _, registered_handlers = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {
    "handler_count": len(registered_handlers),
    "all_are_module_level_handler": all(h is shutdown_module._handle_shutdown_signal for h in registered_handlers),  # pyright: ignore[reportPrivateUsage]
  }


def _join_shutdown_thread() -> None:
  for thread in threading.enumerate():
    if thread.name == _SHUTDOWN_THREAD_NAME:
      thread.join(timeout=15.0)


def _drive_and_join(action: Callable[[], None]) -> None:
  try:
    action()
    _join_shutdown_thread()
  except KeyboardInterrupt:
    _join_shutdown_thread()


def signal_ladder_climbs_all_four_rungs() -> dict[str, object]:
  gate = threading.Event()

  def hold_the_pass_open(trails: tuple[ExceptionTrail, ...]) -> None:
    # Bounded, so a bug here fails the subprocess rather than hanging it.
    gate.wait(timeout=15.0)

  # Held strongly by the registry, since it is a closure rather than a bound
  # method -- a weak reference to one would die immediately.
  shutdown_module.register_for_shutdown(hold_the_pass_open, phase=ShutdownPhase.THREADED, required=True)
  shutdown_module.install_shutdown_signal_handlers()

  kinds: list[str] = []
  signal.raise_signal(signal.SIGINT)  # rung 1: start a graceful shutdown
  kinds.append(SHUTDOWN.kind.name)
  signal.raise_signal(signal.SIGINT)  # rung 2: warn, escalating nothing
  kinds.append(SHUTDOWN.kind.name)
  signal.raise_signal(signal.SIGINT)  # rung 3: escalate to FORCED
  kinds.append(SHUTDOWN.kind.name)
  try:
    signal.raise_signal(signal.SIGINT)  # rung 4: defer to the stock handler
    hard_interrupted = False
  except KeyboardInterrupt:
    hard_interrupted = True
  kinds.append(SHUTDOWN.kind.name)

  _drive_and_join(gate.set)
  return {"kinds": kinds, "hard_interrupted": hard_interrupted}


def exit_nudge_short_circuits_past_the_ladder() -> dict[str, object]:
  shutdown_module._exit_nudge_sent = True  # pyright: ignore[reportPrivateUsage]
  shutdown_module.install_shutdown_signal_handlers()

  try:
    signal.raise_signal(signal.SIGINT)
    hard_interrupted = False
  except KeyboardInterrupt:
    hard_interrupted = True

  return {
    "hard_interrupted": hard_interrupted,
    "shutdown_kind": SHUTDOWN.kind.name,
    "confirm_ticket": next(shutdown_module._confirm_counter),  # pyright: ignore[reportPrivateUsage]
  }


def _first_teardown(trails: tuple[ExceptionTrail, ...]) -> None:
  pass


def _second_teardown(trails: tuple[ExceptionTrail, ...]) -> None:
  pass


def shutdown_output_is_written_to_fd_2() -> dict[str, object]:
  shutdown_module.register_for_shutdown(_first_teardown, phase=ShutdownPhase.THREADED)
  shutdown_module.register_for_shutdown(_second_teardown, phase=ShutdownPhase.THREADED)
  shutdown_module.install_shutdown_signal_handlers()

  _drive_and_join(lambda: signal.raise_signal(signal.SIGINT))
  return {"shutdown_kind": SHUTDOWN.kind.name}


def _report_at_exit(result: dict[str, object]) -> None:
  atexit.unregister(shutdown_module._join_pass_at_exit)  # pyright: ignore[reportPrivateUsage]
  atexit.register(lambda: print(json.dumps(result), flush=True))
  atexit.register(shutdown_module._join_pass_at_exit)  # pyright: ignore[reportPrivateUsage]


def required_callback_completes_when_main_returns_on_shutdown() -> dict[str, object]:
  result: dict[str, object] = {"flush_ran": False}

  def slow_required_flush(trails: tuple[ExceptionTrail, ...]) -> None:
    time.sleep(0.5)
    result["flush_ran"] = True

  shutdown_module.register_for_shutdown(slow_required_flush, phase=ShutdownPhase.THREADED, required=True)
  shutdown_module.install_shutdown_signal_handlers()

  async def main() -> None:
    asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGINT)
    await SHUTDOWN

  asyncio.run(main())
  _report_at_exit(result)
  return result


def tail_after_shutdown_complete_runs_uninterrupted() -> dict[str, object]:
  result: dict[str, object] = {"flush_ran": False, "tail_ran": False, "tail_interrupted": False}

  def slow_required_flush(trails: tuple[ExceptionTrail, ...]) -> None:
    time.sleep(0.5)
    result["flush_ran"] = True

  shutdown_module.register_for_shutdown(slow_required_flush, phase=ShutdownPhase.THREADED, required=True)
  shutdown_module.install_shutdown_signal_handlers()

  async def main() -> None:
    asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGINT)
    await SHUTDOWN
    await SHUTDOWN_COMPLETE
    try:
      await asyncio.sleep(0.3)  # the tail: long enough that an immediate nudge would land in it
      result["tail_ran"] = True
    except KeyboardInterrupt:
      result["tail_interrupted"] = True
      raise

  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    result["tail_interrupted"] = True
  return result


def hung_optional_callback_does_not_hold_exit() -> dict[str, object]:
  result: dict[str, object] = {"exited_within_budget": False}
  t0 = time.monotonic()

  def hangs_forever(trails: tuple[ExceptionTrail, ...]) -> None:
    threading.Event().wait()

  shutdown_module.register_for_shutdown(hangs_forever, phase=ShutdownPhase.THREADED, required=False)
  shutdown_module.install_shutdown_signal_handlers()

  async def main() -> None:
    asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGINT)
    await SHUTDOWN

  asyncio.run(main())

  # Sampled from atexit, after the library's exit-time join of the pass (see
  # _report_at_exit for the ordering): the figure is how long the hung callback
  # held the process, and must sit inside the graceful budget plus a little for
  # the join/exit machinery.
  def sample() -> None:
    result["exited_within_budget"] = time.monotonic() - t0 < shutdown_module._GRACEFUL_BUDGET_SECS + 2.0  # pyright: ignore[reportPrivateUsage]

  atexit.unregister(shutdown_module._join_pass_at_exit)  # pyright: ignore[reportPrivateUsage]
  atexit.register(lambda: (sample(), print(json.dumps(result), flush=True)))
  atexit.register(shutdown_module._join_pass_at_exit)  # pyright: ignore[reportPrivateUsage]
  return result


def hard_interrupt_escapes_a_wedged_required_callback() -> dict[str, object]:
  result: dict[str, object] = {"hard_interrupted": False, "exited": False}

  def wedged(trails: tuple[ExceptionTrail, ...]) -> None:
    threading.Event().wait()

  shutdown_module.register_for_shutdown(wedged, phase=ShutdownPhase.THREADED, required=True)
  shutdown_module.install_shutdown_signal_handlers()

  def mark_exited() -> None:
    result["exited"] = True
    print(json.dumps(result), flush=True)

  atexit.register(mark_exited)

  for _ in range(3):
    signal.raise_signal(signal.SIGINT)
  try:
    signal.raise_signal(signal.SIGINT)
  except KeyboardInterrupt:
    result["hard_interrupted"] = True
  return result


_SCENARIOS = {
  "hung_optional_callback_does_not_hold_exit": hung_optional_callback_does_not_hold_exit,
  "hard_interrupt_escapes_a_wedged_required_callback": hard_interrupt_escapes_a_wedged_required_callback,
  "required_callback_completes_when_main_returns_on_shutdown": required_callback_completes_when_main_returns_on_shutdown,
  "tail_after_shutdown_complete_runs_uninterrupted": tail_after_shutdown_complete_runs_uninterrupted,
  "sigint_is_always_registered": sigint_is_always_registered,
  "windows_registers_sigbreak_when_available_and_not_sigterm": windows_registers_sigbreak_when_available_and_not_sigterm,
  "posix_registers_sigterm": posix_registers_sigterm,
  "registers_the_module_level_handler": registers_the_module_level_handler,
  "signal_ladder_climbs_all_four_rungs": signal_ladder_climbs_all_four_rungs,
  "exit_nudge_short_circuits_past_the_ladder": exit_nudge_short_circuits_past_the_ladder,
  "shutdown_output_is_written_to_fd_2": shutdown_output_is_written_to_fd_2,
}


if __name__ == "__main__":
  # Wrapped because the exit nudge can outlive the scenario that provoked it.
  # `_attempt_early_exit` calls `interrupt_main()` from *inside* the shutdown
  # thread, immediately before that thread ends, so the thread can already be
  # joined and gone while the simulated SIGINT is still pending delivery to this
  # (main) thread. `_drive_and_join`'s own `except KeyboardInterrupt` therefore
  # cannot be relied on to catch it: the interrupt is free to land after the
  # scenario function has returned, anywhere in the statements below -- during
  # `json.dumps`, during the write, or between the write and interpreter exit.
  # Left uncaught it would kill the subprocess with a non-zero status for a
  # reason that has nothing to do with the behaviour under test, and
  # `test_shutdown.py` hard-asserts `proc.returncode == 0`. This is the same
  # swallow `_optimized_scenarios.py` performs inside its individual scenario
  # bodies, applied one level further out, where a per-scenario wrapper can no
  # longer reach.
  #
  # `result` is pre-seeded with a self-describing sentinel so that stdout always
  # carries a parseable JSON line, whichever side of the print the interrupt
  # lands on. Arriving *after* the print, it costs nothing: the correct line is
  # already out, and the parent reads the last line of stdout, so the duplicate
  # emitted below is byte-identical and harmless. Arriving *before* it, the
  # sentinel is what gets printed -- deliberately not silence, because the
  # parent's `proc.stdout.strip().splitlines()[-1]` would raise `IndexError` on
  # empty output and report a scenario that never ran as an unreadable harness
  # crash, whereas a sentinel line fails the assertion that actually cares, with
  # the reason spelled out in it.
  scenario_name = sys.argv[1]
  result: dict[str, object] = {"error": "KeyboardInterrupt landed before the scenario returned its result"}
  try:
    result = _SCENARIOS[scenario_name]()
    # Flushed eagerly so the line is out of the buffer at the earliest possible
    # moment, rather than at interpreter exit with a pending interrupt still
    # able to beat it there.
    print(json.dumps(result), flush=True)
  except KeyboardInterrupt:
    print(json.dumps(result), flush=True)
