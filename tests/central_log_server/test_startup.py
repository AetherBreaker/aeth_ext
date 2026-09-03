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

# One "start" ping from `main`'s own early call, one plain ping from
# `run_heartbeat_async`'s initial (`send_start=False`) call.
_EXPECTED_HEARTBEAT_PING_COUNT = 2


def _run_optimized(scenario_name: str, *args: str) -> Mapping[str, object]:
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
  def test_is_none_under_normal_debug_mode(self) -> None:
    # First party imports
    from aeth_ext.central_log_server import startup

    assert startup.HEARTBEAT_FILE is None

  def test_is_a_real_heartbeat_txt_path_outside_debug_mode(self) -> None:
    result = _run_optimized("heartbeat_file_is_a_real_path_outside_debug_mode")

    assert result == {"heartbeat_file_is_none": False, "heartbeat_file_name": "heartbeat.txt"}


class TestMain:
  def test_boots_every_component_and_shuts_down_cleanly_on_fatal_event(self, tmp_path: Path) -> None:
    result = _run_main_scenario("main_boots_and_shuts_down_cleanly", str(tmp_path))

    assert result["completed"] is True

  def test_sends_one_start_heartbeat_before_scheduling_the_periodic_task(self, tmp_path: Path) -> None:
    result = _run_main_scenario("main_boots_and_shuts_down_cleanly", str(tmp_path))

    (send_heartbeat_call,) = result["send_heartbeat_calls"]  # pyright: ignore[reportGeneralTypeIssues]
    assert send_heartbeat_call["start"] is True
    assert send_heartbeat_call["slug"] is None

    (run_heartbeat_async_call,) = result["run_heartbeat_async_calls"]  # pyright: ignore[reportGeneralTypeIssues]
    assert run_heartbeat_async_call["send_start"] is False
    assert run_heartbeat_async_call["slug"] is None

  def test_heartbeat_slug_auto_detection_resolves_to_central_log_server_end_to_end(self, tmp_path: Path) -> None:
    result = _run_main_scenario("main_boots_with_real_heartbeat_resolution", str(tmp_path))

    assert result["completed"] is True
    ping_calls: list[Mapping[str, object]] = result["ping_calls"]  # pyright: ignore[reportAssignmentType]
    assert len(ping_calls) == _EXPECTED_HEARTBEAT_PING_COUNT
    for call in ping_calls:
      assert call["url"] == "https://hc-ping.com/test-pingkey/central-log-server"
      assert call["autoprovision"] is True
    assert [call["start"] for call in ping_calls] == [True, False]
