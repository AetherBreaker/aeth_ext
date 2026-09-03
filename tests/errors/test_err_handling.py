# Standard library imports
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.errors import err_handling

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping

_SCENARIOS_SCRIPT = Path(__file__).parent / "_optimized_scenarios.py"


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


class TestNoOpUnderNormalDebugMode:
  def test_handle_fatal_exc_sync_bare_returns_the_original_function(self) -> None:
    def func() -> None:
      pass

    assert err_handling.handle_fatal_exc_sync(func) is func

  def test_handle_fatal_exc_async_bare_returns_the_original_function(self) -> None:
    async def func() -> None:
      pass

    assert err_handling.handle_fatal_exc_async(func) is func

  def test_report_exc_lets_exceptions_propagate_unmodified(self) -> None:
    with pytest.raises(ValueError, match="boom"), err_handling.report_exc("label"):
      raise ValueError("boom")

  def test_report_exc_no_exception_is_a_no_op(self) -> None:
    with err_handling.report_exc("label"):
      pass


class TestHandleFatalExcSyncUnderOptimizedMode:
  def test_generic_exception_is_alerted_and_swallowed(self) -> None:
    result = _run_optimized("handle_fatal_exc_sync_generic_exception")

    assert result == {"returned": None, "alert_calls": 1, "push_alert_calls": 1, "shutdown_kind": "FATAL"}

  def test_cancelled_error_propagates_without_alerting(self) -> None:
    result = _run_optimized("handle_fatal_exc_sync_cancelled_error")

    assert result == {"propagated": True, "alert_calls": 0}


class TestHandleFatalExcAsyncUnderOptimizedMode:
  def test_generic_exception_is_alerted_and_swallowed(self) -> None:
    result = _run_optimized("handle_fatal_exc_async_generic_exception")

    assert result == {"returned": None, "alert_calls": 1, "shutdown_kind": "FATAL"}

  def test_cancelled_error_propagates_without_alerting(self) -> None:
    result = _run_optimized("handle_fatal_exc_async_cancelled_error")

    assert result == {"propagated": True, "alert_calls": 0}

  def test_generator_exit_returns_none_without_alerting(self) -> None:
    result = _run_optimized("handle_fatal_exc_async_generator_exit")

    assert result == {"returned": None, "alert_calls": 0, "shutdown_kind": "RUNNING"}


class TestReportExcUnderOptimizedMode:
  def test_default_swallows_the_exception_after_alerting(self) -> None:
    result = _run_optimized("report_exc_default_swallows")

    assert result == {"alert_calls": 1, "shutdown_kind": "FATAL"}

  def test_reraise_true_propagates_after_alerting(self) -> None:
    result = _run_optimized("report_exc_reraise_true_propagates")

    assert result == {"propagated": True, "alert_calls": 1}

  def test_cancelled_error_always_propagates_without_alerting(self) -> None:
    result = _run_optimized("report_exc_cancelled_error_always_propagates")

    assert result == {"propagated": True, "alert_calls": 0}


class TestTriggerShutdownIsANoOpUnderNormalDebugMode:
  def test_does_not_alert_or_request_shutdown(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(err_handling, "send_alert_email", lambda *a, **k: calls.append((a, k)))

    err_handling.trigger_shutdown("reason", "details")

    assert calls == []


class TestTriggerShutdownUnderOptimizedMode:
  def test_alerts_and_requests_a_fatal_shutdown_by_default(self) -> None:
    result = _run_optimized("trigger_shutdown_alerts_and_requests_a_shutdown")

    assert result == {
      "alert_calls": 1,
      "push_alert_calls": 1,
      "priority": err_handling._FATAL_PUSH_PRIORITY,  # pyright: ignore[reportPrivateUsage]
      "mentions_reason": True,
      "shutdown_kind": "FATAL",
    }


class TestAlertExceptionIsANoOpUnderNormalDebugMode:
  def test_does_not_call_send_alert_email(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(err_handling, "send_alert_email", lambda *a, **k: calls.append((a, k)))

    try:
      raise ValueError("boom")
    except ValueError as e:
      err_handling.alert_exception("some.label", e)

    assert calls == []


class TestAlertExceptionUnderOptimizedMode:
  def test_sends_an_alert_without_requesting_a_shutdown(self) -> None:
    result = _run_optimized("alert_exception_sends_without_requesting_shutdown")

    assert result == {"alert_calls": 1, "shutdown_kind": "RUNNING"}

  def test_alert_email_content_includes_the_exception_and_label(self) -> None:
    result = _run_optimized("alert_exception_content_includes_exception_and_label")

    # "Alert: [tests] " is the standardized prefix alert() adds to every
    # subject (see its docstring); "tests" comes from _resolve_program_name's
    # fallback to the entrypoint package's directory name, which for these
    # subprocess scenarios is always this repo's `tests/` package.
    assert result["subject"] == "Alert: [tests] Non-fatal exception in my.custom.label"
    assert result["mentions_exception"] is True

  def test_does_not_catch_or_affect_the_active_exception(self) -> None:
    result = _run_optimized("alert_exception_does_not_affect_control_flow")

    assert result == {"propagated": True, "alert_calls": 1}


class TestPushAlertPriorityUnderOptimizedMode:
  def test_fatal_exception_uses_high_push_priority(self) -> None:
    result = _run_optimized("fatal_exception_sends_push_alert_with_high_priority")

    assert result == {"push_alert_calls": 1, "priority": err_handling._FATAL_PUSH_PRIORITY}  # pyright: ignore[reportPrivateUsage]

  def test_alert_exception_uses_the_default_normal_push_priority(self) -> None:
    result = _run_optimized("alert_exception_sends_push_alert_with_normal_priority")

    assert result == {"push_alert_calls": 1, "priority": 0}


class TestReportExcAlertsAndRequestsFatalShutdownUnderOptimizedMode:
  def test_alerts_and_requests_a_fatal_shutdown(self) -> None:
    result = _run_optimized("report_exc_alerts_and_requests_fatal_shutdown")

    assert result == {
      "alert_calls": 1,
      "push_alert_calls": 1,
      "priority": err_handling._FATAL_PUSH_PRIORITY,  # pyright: ignore[reportPrivateUsage]
      "mentions_reason": True,
      "shutdown_kind": "FATAL",
    }

  def test_sets_the_current_fatal_trail(self) -> None:
    result = _run_optimized("report_exc_sets_the_current_fatal_trail")

    # Run as a script (`python -O script.py`), so the raising frame's own module is "__main__".
    assert result == {"trail_count": 1, "origin_module": "__main__"}

  def test_still_shuts_down_when_trail_building_fails(self) -> None:
    result = _run_optimized("report_exc_still_shuts_down_when_trail_building_fails")

    assert result == {"shutdown_kind": "FATAL", "trail_count": 0}
