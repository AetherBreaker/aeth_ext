"""Tests for `aeth_ext.central_log_server.startup`.

`write_heartbeat` is a no-op under `__debug__ == True` (see the module's
`if not __debug__: ... else: ...` split), and `run_periodic`'s "propagate to
the alert wrapper instead of retrying" behavior only actually fires once an
exception reaches `handle_fatal_exc_async`, which is itself a no-op under
`__debug__ == True`. Both are exercised in a `-O` subprocess, following the
same pattern as `tests/errors/test_err_handling.py`.

Each scenario is a real, importable function in `_optimized_scenarios.py`
(selected here by name) rather than a code string embedded in this file --
code inside a string is invisible to IDE rename-symbol tooling regardless of
which file holds it, so keeping scenarios as genuine parsed Python source is
what actually keeps them rename-resilient.
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

_MIN_CALLS_TO_OBSERVE = 3

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


class TestWriteHeartbeatUnderOptimizedMode:
  def test_writes_current_timestamp_to_the_heartbeat_file(self, tmp_path: Path):
    heartbeat_file = tmp_path / "heartbeat.txt"

    result = _run_optimized("write_heartbeat_writes_timestamp", str(heartbeat_file))

    assert result == {"wrote_something": True}
    assert heartbeat_file.exists()
    assert heartbeat_file.read_text().strip() != ""

  def test_failure_is_logged_not_raised(self, tmp_path: Path):
    missing_dir_file = tmp_path / "does_not_exist" / "heartbeat.txt"

    result = _run_optimized("write_heartbeat_failure_is_logged_not_raised", str(missing_dir_file))

    assert result == {"survived": True}


class TestRunPeriodicUnderOptimizedMode:
  def test_successful_calls_repeat_without_alerting(self):
    result = _run_optimized("run_periodic_successful_calls_repeat_without_alerting")

    assert int(result["call_count"]) >= _MIN_CALLS_TO_OBSERVE  # pyright: ignore[reportArgumentType]
    assert result["alert_calls"] == 0

  def test_failing_func_alerts_on_first_failure_instead_of_retrying(self):
    result = _run_optimized("run_periodic_failing_func_alerts_on_first_failure")

    assert result == {"call_count": 1, "alert_calls": 1, "returned": None}


class TestMain:
  def test_boots_every_component_and_shuts_down_cleanly_on_fatal_event(self, tmp_path: Path):
    """Regression coverage for `startup.main`'s real orchestration.

    Runs the actual production boot sequence (id registry, writer thread, TCP
    reader server, in-loop web viewer server, periodic heartbeat task) against
    real ephemeral ports and a real temp log directory, then confirms it
    reaches a clean, non-hanging shutdown once `FATAL_EVENT` is set.
    """
    result = _run_main_scenario("main_boots_and_shuts_down_cleanly", str(tmp_path))

    assert result == {"completed": True}
