"""Real, importable `-O` subprocess scenarios for `register_for_shutdown`'s trail-passing behavior
and `get_current_fatal_trail` (`aeth_ext.errors.shutdown`).

Written as genuine Python source, not a `python -c` string, so IDE rename-symbol tooling can track
references -- same convention as `_shutdown_signal_scenarios.py` and `_optimized_scenarios.py`.
`register_for_shutdown`'s trail-passing only happens during a real driven shutdown, and
`get_current_fatal_trail` reads the process-wide, one-shot `_current_fatal_trail` slot, so both need
this repo's existing `-O` isolated-subprocess pattern.
"""

# Standard library imports
import json
import sys
import time

# First party imports
from aeth_ext.errors import shutdown as shutdown_module
from aeth_ext.errors.exception_trail import build_exception_trail
from aeth_ext.errors.shutdown import ShutdownKind, ShutdownPhase

assert not __debug__, "this harness must run under python -O"


def get_current_fatal_trail_is_none_before_any_shutdown() -> dict[str, object]:
  return {"trail": shutdown_module.get_current_fatal_trail()}


def get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown() -> dict[str, object]:
  try:
    raise ValueError("boom")
  except ValueError as e:
    trail = build_exception_trail(e)
    shutdown_module._set_current_fatal_trail(trail)  # pyright: ignore[reportPrivateUsage]

  retrieved = shutdown_module.get_current_fatal_trail()
  return {"same_object": retrieved is trail, "origin_module": retrieved.origin.module if retrieved else None}


def zero_arg_callback_is_invoked_with_no_arguments() -> dict[str, object]:
  calls: list[object] = []

  def zero_arg() -> None:
    calls.append("called")

  shutdown_module.register_for_shutdown(zero_arg, phase=ShutdownPhase.THREADED)
  shutdown_module.run_shutdown(ShutdownKind.GRACEFUL)
  # run_shutdown() hands the threaded pass to a daemon thread and returns immediately; give it a
  # moment to actually run before checking. The threaded pass always ends by nudging the main
  # thread via interrupt_main() (D-I3/D-I4), which races this sleep -- by the time it lands, the
  # callback has already run (the nudge is the pass's very last step), so catching the one-shot
  # KeyboardInterrupt here loses no information. See _optimized_scenarios.py for the same pattern.
  try:
    time.sleep(0.5)
  except KeyboardInterrupt:
    pass
  return {"called": calls == ["called"]}


def one_arg_callback_receives_none_when_no_trail_is_set() -> dict[str, object]:
  received: list[object] = []

  def one_arg(trail: object) -> None:
    received.append(trail)

  shutdown_module.register_for_shutdown(one_arg, phase=ShutdownPhase.THREADED)
  shutdown_module.run_shutdown(ShutdownKind.GRACEFUL)
  try:
    time.sleep(0.5)
  except KeyboardInterrupt:
    pass
  return {"received_none": received == [None]}


def one_arg_callback_receives_the_real_trail_when_fatal() -> dict[str, object]:
  received: list[object] = []

  def one_arg(trail: object) -> None:
    received.append(trail)

  shutdown_module.register_for_shutdown(one_arg, phase=ShutdownPhase.THREADED)
  try:
    raise ValueError("boom")
  except ValueError as e:
    shutdown_module._set_current_fatal_trail(build_exception_trail(e))  # pyright: ignore[reportPrivateUsage]

  shutdown_module.run_shutdown(ShutdownKind.FATAL)
  try:
    time.sleep(0.5)
  except KeyboardInterrupt:
    pass
  return {"received_a_trail": len(received) == 1 and received[0] is not None}


_SCENARIOS = {
  "get_current_fatal_trail_is_none_before_any_shutdown": get_current_fatal_trail_is_none_before_any_shutdown,
  "get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown": (
    get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown
  ),
  "zero_arg_callback_is_invoked_with_no_arguments": zero_arg_callback_is_invoked_with_no_arguments,
  "one_arg_callback_receives_none_when_no_trail_is_set": one_arg_callback_receives_none_when_no_trail_is_set,
  "one_arg_callback_receives_the_real_trail_when_fatal": one_arg_callback_receives_the_real_trail_when_fatal,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps({k: (v if not hasattr(v, "origin") else True) for k, v in result.items()}))
