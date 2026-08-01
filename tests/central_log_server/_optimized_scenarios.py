"""Real, importable scenario functions for `-O` (`__debug__ == False`) subprocess tests.

Written as genuine Python source -- not string-embedded code passed to
`python -c` -- so IDE rename-symbol tooling can track and update references
to the symbols under test here exactly like any other file in the suite.
Each scenario is selected by name via `argv[1]` (with any further positional
args forwarded as its arguments) and this module is run in a fresh `-O`
interpreter by `_run_optimized` in `test_startup.py`, since `write_heartbeat`
is only meaningfully different from a no-op under `__debug__ == False`, and
`run_periodic`'s "alert instead of retry" behavior only fires once an
exception reaches `handle_fatal_exc_async`, itself a no-op under
`__debug__ == True`.
"""

# Standard library imports
import asyncio
import json
import sys
from pathlib import Path

# First party imports
from aeth_ext.central_log_server import startup
from aeth_ext.errors import err_handling

assert not __debug__, "this harness must run under python -O"

alert_calls: list[tuple[str, str]] = []
err_handling.send_alert_email = lambda subject, content: alert_calls.append((subject, content))


def write_heartbeat_writes_timestamp(heartbeat_path: str) -> dict[str, object]:
  startup.HEARTBEAT_FILE = Path(heartbeat_path)
  startup.write_heartbeat()
  return {"wrote_something": True}


def write_heartbeat_failure_is_logged_not_raised(heartbeat_path: str) -> dict[str, object]:
  """A HEARTBEAT_FILE pointing at a directory that doesn't exist should be
  caught and logged inside write_heartbeat itself, not propagate."""
  startup.HEARTBEAT_FILE = Path(heartbeat_path)
  startup.write_heartbeat()  # must not raise
  return {"survived": True}


def run_periodic_successful_calls_repeat_without_alerting() -> dict[str, object]:
  call_count = 0

  def func() -> None:
    nonlocal call_count
    call_count += 1

  async def go() -> None:
    # Cancellation (via wait_for's timeout) is the only way to stop
    # run_periodic's infinite loop without it looking like a task failure --
    # CancelledError is explicitly exempted from the alert path.
    try:
      await asyncio.wait_for(startup.run_periodic(0.01, func), timeout=0.1)
    except TimeoutError:
      pass

  asyncio.run(go())
  return {"call_count": call_count, "alert_calls": len(alert_calls)}


def run_periodic_failing_func_alerts_on_first_failure() -> dict[str, object]:
  call_count = 0

  def func() -> None:
    nonlocal call_count
    call_count += 1
    raise ValueError("heartbeat is broken")

  returned = asyncio.run(startup.run_periodic(0.01, func))
  return {"call_count": call_count, "alert_calls": len(alert_calls), "returned": returned}


_SCENARIOS = {
  "write_heartbeat_writes_timestamp": write_heartbeat_writes_timestamp,
  "write_heartbeat_failure_is_logged_not_raised": write_heartbeat_failure_is_logged_not_raised,
  "run_periodic_successful_calls_repeat_without_alerting": run_periodic_successful_calls_repeat_without_alerting,
  "run_periodic_failing_func_alerts_on_first_failure": run_periodic_failing_func_alerts_on_first_failure,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  scenario_args = sys.argv[2:]
  result = _SCENARIOS[scenario_name](*scenario_args)
  print(json.dumps(result))
