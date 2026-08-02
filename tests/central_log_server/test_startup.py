"""Tests for `aeth_ext.central_log_server.startup`.

`HEARTBEAT_FILE` is only a real path (rather than `None`) under
`__debug__ == False`, and `FATAL_EVENT` is a one-shot `aiologic.Event` that
cannot be reset once set, so a real boot-then-shutdown cycle of `startup.main`
needs its own interpreter. Both are exercised in subprocesses, following the
same pattern used across the rest of this suite.

Each scenario is a real, importable function in `_optimized_scenarios.py` /
`_main_scenarios.py` (selected here by name) rather than a code string
embedded in this file -- code inside a string is invisible to IDE
rename-symbol tooling regardless of which file holds it, so keeping scenarios
as genuine parsed Python source is what actually keeps them rename-resilient.

The heartbeat primitives themselves (writing the file, resolving the ping
URL, `/start` vs. plain pings, stopping on `FATAL_EVENT`) are already covered
by `tests/monitoring/`; the tests here only cover startup.py's own wiring --
`HEARTBEAT_FILE`'s debug-gating and the exact arguments `main` passes to
`send_heartbeat`/`run_heartbeat_async`.
"""

# Standard library imports
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping

_SCENARIOS_SCRIPT = Path(__file__).parent / "_optimized_scenarios.py"
_MAIN_SCENARIOS_SCRIPT = Path(__file__).parent / "_main_scenarios.py"


def _run_optimized(scenario_name: str, *args: str) -> Mapping[str, object]:
  """Run the named scenario from `_optimized_scenarios.py` in a fresh `-O` subprocess."""
  env = dict(os.environ)
  env.setdefault("ALERTS_EMAIL_PWD", "test-password")

  proc = subprocess.run(
    [sys.executable, "-O", str(_SCENARIOS_SCRIPT), scenario_name, *args],
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
    check=False,
  )

  assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

  return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_main_scenario(scenario_name: str, *args: str) -> Mapping[str, object]:
  """Run the named scenario from `_main_scenarios.py` in a fresh subprocess.

  `FATAL_EVENT` (an `aiologic.Event`) is one-shot and process-global, so a
  real boot-then-shutdown cycle of `startup.main` needs its own interpreter,
  same rationale as `_run_optimized` above.
  """
  env = dict(os.environ)
  env.setdefault("ALERTS_EMAIL_PWD", "test-password")

  proc = subprocess.run(
    [sys.executable, str(_MAIN_SCENARIOS_SCRIPT), scenario_name, *args],
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
    check=False,
  )

  assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

  return json.loads(proc.stdout.strip().splitlines()[-1])


class TestHeartbeatFileLocation:
  def test_is_none_under_normal_debug_mode(self):
    # First party imports
    from aeth_ext.central_log_server import startup

    assert startup.HEARTBEAT_FILE is None

  def test_is_a_real_heartbeat_txt_path_outside_debug_mode(self):
    result = _run_optimized("heartbeat_file_is_a_real_path_outside_debug_mode")

    assert result == {"heartbeat_file_is_none": False, "heartbeat_file_name": "heartbeat.txt"}


class TestMain:
  def test_boots_every_component_and_shuts_down_cleanly_on_fatal_event(self, tmp_path: Path):
    """Regression coverage for `startup.main`'s real orchestration.

    Runs the actual production boot sequence (id registry, writer thread, TCP
    reader server, in-loop web viewer server, heartbeat) against real
    ephemeral ports and a real temp log directory, then confirms it reaches a
    clean, non-hanging shutdown once `FATAL_EVENT` is set.
    """
    result = _run_main_scenario("main_boots_and_shuts_down_cleanly", str(tmp_path))

    assert result["completed"] is True

  def test_sends_one_start_heartbeat_before_scheduling_the_periodic_task(self, tmp_path: Path):
    """`main` sends its own "start" ping as early as possible in boot, then
    hands off to `run_heartbeat_async` with `send_start=False` so the
    periodic task doesn't send a second, redundant start ping."""
    result = _run_main_scenario("main_boots_and_shuts_down_cleanly", str(tmp_path))

    (send_heartbeat_call,) = result["send_heartbeat_calls"]  # pyright: ignore[reportGeneralTypeIssues]
    assert send_heartbeat_call["start"] is True
    assert send_heartbeat_call["slug"] == "central-log-server"

    (run_heartbeat_async_call,) = result["run_heartbeat_async_calls"]  # pyright: ignore[reportGeneralTypeIssues]
    assert run_heartbeat_async_call["send_start"] is False
    assert run_heartbeat_async_call["slug"] == "central-log-server"
