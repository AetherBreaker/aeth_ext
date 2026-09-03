# Standard library imports
import json
import os
import subprocess
import sys
from pathlib import Path
from sys import platform
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
import aeth_ext

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping

_SCENARIOS_SCRIPT = Path(__file__).parent / "errors" / "_shutdown_signal_scenarios.py"


def _run_optimized(scenario_name: str) -> Mapping[str, object]:
  env = dict(os.environ)
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

  return json.loads(proc.stdout.strip().splitlines()[-1])


class TestInstallShutdownSignalHandlers:
  def test_sigint_is_always_registered(self) -> None:
    result = _run_optimized("sigint_is_always_registered")

    assert result == {"sigint_registered": True}

  @pytest.mark.skipif(platform != "win32", reason="Windows-specific signal set")
  def test_windows_registers_sigbreak_when_available_and_not_sigterm(self) -> None:
    result = _run_optimized("windows_registers_sigbreak_when_available_and_not_sigterm")

    assert result["sigterm_registered"] is False
    if result["has_sigbreak"]:
      assert result["sigbreak_registered"] is True

  @pytest.mark.skipif(platform == "win32", reason="POSIX-specific signal set")
  def test_posix_registers_sigterm(self) -> None:
    result = _run_optimized("posix_registers_sigterm")

    assert result == {"sigterm_registered": True}

  def test_registers_the_module_level_handler(self) -> None:
    result = _run_optimized("registers_the_module_level_handler")

    assert result["handler_count"]
    assert result["all_are_module_level_handler"] is True


class TestInitializeInstallSignalHandlersFlag:
  def test_default_registers_signal_handlers(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(aeth_ext, "install_shutdown_signal_handlers", lambda: calls.append(True))

    aeth_ext.initialize(logging=False, run_monkey_patches=False)

    assert calls == [True]

  def test_opted_out_skips_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(aeth_ext, "install_shutdown_signal_handlers", lambda: calls.append(True))

    aeth_ext.initialize(logging=False, run_monkey_patches=False, install_signal_handlers=False)

    assert calls == []
