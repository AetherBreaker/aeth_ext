"""Real, importable `-O` subprocess scenarios for `register_for_shutdown`'s trail-passing behavior
and `get_current_fatal_trails` (`aeth_ext.errors.shutdown`).

Written as genuine Python source, not a `python -c` string, so IDE rename-symbol tooling can track
references -- same convention as `_shutdown_signal_scenarios.py` and `_optimized_scenarios.py`.
`register_for_shutdown`'s trail-passing only happens during a real driven shutdown, and
`get_current_fatal_trails` reads the process-wide, one-shot `_current_fatal_trails` slot, so both
need this repo's existing `-O` isolated-subprocess pattern.
"""

# Standard library imports
import json
import sys

# Third party imports
# Local imports -- shares the join-not-sleep synchronization idiom, not just its scenario
# harness convention; see _drive_and_join's docstring for why joining is what makes this
# deterministic.
from _shutdown_signal_scenarios import _drive_and_join  # pyright: ignore[reportPrivateUsage]

# First party imports
from aeth_ext.errors import shutdown as shutdown_module
from aeth_ext.errors.exception_trail import build_exception_trail
from aeth_ext.errors.shutdown import ShutdownKind, ShutdownPhase

assert not __debug__, "this harness must run under python -O"


def get_current_fatal_trails_is_empty_before_any_shutdown() -> dict[str, object]:
  return {"trail_count": len(shutdown_module.get_current_fatal_trails())}


def get_current_fatal_trails_returns_the_set_trail_after_fatal_shutdown() -> dict[str, object]:
  try:
    raise ValueError("boom")
  except ValueError as e:
    trail = build_exception_trail(e)
    shutdown_module._set_current_fatal_trail(trail)  # pyright: ignore[reportPrivateUsage]

  retrieved = shutdown_module.get_current_fatal_trails()
  return {
    "trail_count": len(retrieved),
    "same_object": retrieved[0] is trail,
    "origin_module": retrieved[0].origin.module,
  }


def callback_receives_an_empty_tuple_when_no_trail_is_set() -> dict[str, object]:
  received: list[object] = []

  def callback(trails: object) -> None:
    received.append(trails)

  shutdown_module.register_for_shutdown(callback, phase=ShutdownPhase.THREADED)
  _drive_and_join(lambda: shutdown_module.run_shutdown(ShutdownKind.GRACEFUL))
  return {"received_empty_tuple": received == [()]}


def callback_receives_the_trail_tuple_when_fatal() -> dict[str, object]:
  received: list[object] = []

  def callback(trails: object) -> None:
    received.append(trails)

  shutdown_module.register_for_shutdown(callback, phase=ShutdownPhase.THREADED)
  try:
    raise ValueError("boom")
  except ValueError as e:
    shutdown_module._set_current_fatal_trail(build_exception_trail(e))  # pyright: ignore[reportPrivateUsage]

  _drive_and_join(lambda: shutdown_module.run_shutdown(ShutdownKind.FATAL))
  return {"received_one_trail": len(received) == 1 and len(received[0]) == 1}  # type: ignore[arg-type]


def interrupt_callback_receives_an_empty_tuple_when_no_trail_is_set() -> dict[str, object]:
  """`_run_interrupt_pass` duplicates `_run_threaded_pass`'s trail-dispatch logic rather than
  sharing it (the interrupt pass must stay lock-free/signal-safe), so this exercises that
  branch independently -- a regression isolated to it would otherwise pass the whole suite.
  """
  received: list[object] = []

  def callback(trails: object) -> None:
    received.append(trails)

  shutdown_module.register_for_shutdown(callback, phase=ShutdownPhase.INTERRUPT)
  # The interrupt pass runs inline inside run_shutdown() itself, so `received` is already
  # populated by the time it returns -- no thread hand-off to wait on for the callback itself.
  # Still routed through _drive_and_join to swallow the threaded pass's later interrupt_main()
  # nudge, which run_shutdown() always starts regardless of whether anything is registered for it.
  _drive_and_join(lambda: shutdown_module.run_shutdown(ShutdownKind.GRACEFUL))
  return {"received_empty_tuple": received == [()]}


def interrupt_callback_receives_the_trail_tuple_when_fatal() -> dict[str, object]:
  received: list[object] = []

  def callback(trails: object) -> None:
    received.append(trails)

  shutdown_module.register_for_shutdown(callback, phase=ShutdownPhase.INTERRUPT)
  try:
    raise ValueError("boom")
  except ValueError as e:
    shutdown_module._set_current_fatal_trail(build_exception_trail(e))  # pyright: ignore[reportPrivateUsage]

  _drive_and_join(lambda: shutdown_module.run_shutdown(ShutdownKind.FATAL))
  return {"received_one_trail": len(received) == 1 and len(received[0]) == 1}  # type: ignore[arg-type]


def a_second_fatal_trail_is_accumulated_not_overwritten() -> dict[str, object]:
  """Two fatal exceptions racing to trigger shutdown must both survive (D-copilot-A):
  `_set_current_fatal_trail` appends under `_fatal_trail_lock` rather than clobbering
  `_current_fatal_trails`, so a callback that hasn't run yet sees every trail landed so far,
  not just the most recent one.
  """
  received: list[object] = []

  def callback(trails: object) -> None:
    received.append(trails)

  shutdown_module.register_for_shutdown(callback, phase=ShutdownPhase.THREADED)
  try:
    raise ValueError("first")
  except ValueError as e:
    shutdown_module._set_current_fatal_trail(build_exception_trail(e))  # pyright: ignore[reportPrivateUsage]
  try:
    raise ValueError("second")
  except ValueError as e:
    shutdown_module._set_current_fatal_trail(build_exception_trail(e))  # pyright: ignore[reportPrivateUsage]

  _drive_and_join(lambda: shutdown_module.run_shutdown(ShutdownKind.FATAL))
  return {"trail_count": len(received[0]) if received else 0}  # type: ignore[arg-type]


_SCENARIOS = {
  "get_current_fatal_trails_is_empty_before_any_shutdown": get_current_fatal_trails_is_empty_before_any_shutdown,
  "get_current_fatal_trails_returns_the_set_trail_after_fatal_shutdown": (
    get_current_fatal_trails_returns_the_set_trail_after_fatal_shutdown
  ),
  "callback_receives_an_empty_tuple_when_no_trail_is_set": callback_receives_an_empty_tuple_when_no_trail_is_set,
  "callback_receives_the_trail_tuple_when_fatal": callback_receives_the_trail_tuple_when_fatal,
  "interrupt_callback_receives_an_empty_tuple_when_no_trail_is_set": (
    interrupt_callback_receives_an_empty_tuple_when_no_trail_is_set
  ),
  "interrupt_callback_receives_the_trail_tuple_when_fatal": interrupt_callback_receives_the_trail_tuple_when_fatal,
  "a_second_fatal_trail_is_accumulated_not_overwritten": a_second_fatal_trail_is_accumulated_not_overwritten,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps({k: (v if not hasattr(v, "origin") else True) for k, v in result.items()}))
