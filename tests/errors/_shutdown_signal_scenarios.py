"""Real, importable scenario functions for `-O` (`__debug__ == False`) subprocess tests
of `aeth_ext.errors.shutdown.install_shutdown_signal_handlers` (D-I3).

Written as genuine Python source -- not string-embedded code passed to
`python -c` -- so IDE rename-symbol tooling can track and update references
to the symbols under test here exactly like any other file in the suite.
Each scenario is selected by name via `argv[1]` and this module is run in a
fresh `-O` interpreter by `_run_optimized` in `test_init.py`, since
`install_shutdown_signal_handlers` is a no-op under `__debug__ == True` (see
its docstring) and `__debug__` cannot be flipped at runtime.
"""

# Standard library imports
import json
import signal
import sys
import threading
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.errors import shutdown as shutdown_module
from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownPhase

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

assert not __debug__, "this harness must run under python -O"

_SHUTDOWN_THREAD_NAME = "aeth-ext-shutdown"
"""The name `run_shutdown` gives the thread it hands the threaded pass to."""


def _patch_signal_signal() -> tuple[list[int], list[object]]:
  """Replace `signal.signal` with a recorder instead of a real registration.

  This is a disposable, single-scenario subprocess, so mutating the real
  `signal` module directly (rather than needing a `monkeypatch` fixture) is
  safe -- nothing else in the process depends on real signal delivery.
  """
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
  """Block until the threaded pass's thread has finished, if one was started."""
  for thread in threading.enumerate():
    if thread.name == _SHUTDOWN_THREAD_NAME:
      thread.join(timeout=15.0)


def _drive_and_join(action: Callable[[], None]) -> None:
  """Run *action*, then wait out the threaded pass it releases or starts.

  Every threaded pass now ends by calling `_thread.interrupt_main()`
  unconditionally, which simulates SIGINT and therefore raises
  `KeyboardInterrupt` on this (main) thread -- the same reason every scenario in
  `_optimized_scenarios.py` wraps its call in `except KeyboardInterrupt: pass`.
  Swallowing it here loses no information: the scenarios below have already
  recorded everything they assert on by the time it can land.

  Joining the shutdown thread is what makes the fd-2 assertions deterministic
  without a single sleep -- it guarantees the end banner has been written before
  the scenario prints its result. The join is repeated in the `except` branch
  because the nudge is sent from inside the thread, just before it finishes, so
  catching the interrupt does not by itself mean the thread is gone.
  """
  try:
    action()
    _join_shutdown_thread()
  except KeyboardInterrupt:
    _join_shutdown_thread()


def signal_ladder_climbs_all_four_rungs() -> dict[str, object]:
  """Four SIGINTs, one rung each: start, warn, force, hard interrupt (D-I3).

  `signal.raise_signal` runs the Python-level handler before it returns (it
  calls `PyErr_CheckSignals()` itself), so the presses below are delivered
  synchronously and in order on this thread -- which is what makes a multi-press
  ladder testable at all, with no polling and no sleeps.

  The gate registrant parks the threaded pass until the last rung has been
  climbed. Without it the pass would finish, set `_exit_nudge_sent`, and send
  presses 2-4 straight out of the ladder's short-circuit instead of into the
  rungs under test here.
  """
  gate = threading.Event()

  def hold_the_pass_open() -> None:
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
  """Our own `interrupt_main()` must bypass the ladder entirely (D-I4).

  `_exit_nudge_sent` is set directly rather than by driving a real teardown to
  completion: the flag's only role here is to be visible to the handler, and
  reaching it naturally would make the test both slower and racy for no added
  coverage.

  With the flag set, a SIGINT must reach `signal.default_int_handler` -- and so
  raise `KeyboardInterrupt` -- without starting a shutdown, without emitting a
  line, and without advancing `_confirm_counter`. The counter read below is the
  strongest of those checks: it is the first ticket, so no rung of the ladder's
  `match` was entered.
  """
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


def _first_teardown() -> None:
  """A no-op threaded registrant, named so the banners have something to count."""


def _second_teardown() -> None:
  """A second no-op threaded registrant."""


def shutdown_output_is_written_to_fd_2() -> dict[str, object]:
  """A small, real graceful shutdown, driven through the ladder's first rung.

  Two trivial registrants rather than none, so the end banner's run/skipped
  figures are non-trivially checkable against the count the start banner
  promised. Triggering via SIGINT rather than by calling `run_shutdown`
  directly is deliberate: the rung-1 line is the only line in the module that
  can be emitted while `_t0` is still unset, and its bare `[shutdown]` prefix is
  part of what this scenario exists to show.
  """
  shutdown_module.register_for_shutdown(_first_teardown, phase=ShutdownPhase.THREADED)
  shutdown_module.register_for_shutdown(_second_teardown, phase=ShutdownPhase.THREADED)
  shutdown_module.install_shutdown_signal_handlers()

  _drive_and_join(lambda: signal.raise_signal(signal.SIGINT))
  return {"shutdown_kind": SHUTDOWN.kind.name}


_SCENARIOS = {
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
