# Standard library imports
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.errors import crash_hooks, err_handling

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator, Mapping

_SCENARIOS_SCRIPT = Path(__file__).parent / "_optimized_scenarios.py"
_SEND_TIMEOUT_SECS = 0.2


def _run_optimized(scenario_name: str) -> tuple[Mapping[str, object], str]:
  """Run *scenario_name* under ``python -O``; return its JSON result and its stderr."""
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

  return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


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
    result, _ = _run_optimized("handle_fatal_exc_sync_generic_exception")

    assert result == {"returned": None, "alert_calls": 1, "push_alert_calls": 1, "shutdown_kind": "FATAL"}

  def test_cancelled_error_propagates_without_alerting(self) -> None:
    result, _ = _run_optimized("handle_fatal_exc_sync_cancelled_error")

    assert result == {"propagated": True, "alert_calls": 0}


class TestHandleFatalExcAsyncUnderOptimizedMode:
  def test_generic_exception_is_alerted_and_swallowed(self) -> None:
    result, _ = _run_optimized("handle_fatal_exc_async_generic_exception")

    assert result == {"returned": None, "alert_calls": 1, "shutdown_kind": "FATAL"}

  def test_cancelled_error_propagates_without_alerting(self) -> None:
    result, _ = _run_optimized("handle_fatal_exc_async_cancelled_error")

    assert result == {"propagated": True, "alert_calls": 0}

  def test_generator_exit_returns_none_without_alerting(self) -> None:
    result, _ = _run_optimized("handle_fatal_exc_async_generator_exit")

    assert result == {"returned": None, "alert_calls": 0, "shutdown_kind": "RUNNING"}


class TestReportExcUnderOptimizedMode:
  def test_default_swallows_the_exception_after_alerting(self) -> None:
    result, _ = _run_optimized("report_exc_default_swallows")

    assert result == {"alert_calls": 1, "shutdown_kind": "FATAL"}

  def test_reraise_true_propagates_after_alerting(self) -> None:
    result, _ = _run_optimized("report_exc_reraise_true_propagates")

    assert result == {"propagated": True, "alert_calls": 1}

  def test_cancelled_error_always_propagates_without_alerting(self) -> None:
    result, _ = _run_optimized("report_exc_cancelled_error_always_propagates")

    assert result == {"propagated": True, "alert_calls": 0}


class TestTriggerShutdownIsANoOpUnderNormalDebugMode:
  def test_does_not_alert_or_request_shutdown(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(err_handling, "send_alert_email", lambda *a, **k: calls.append((a, k)))

    err_handling.trigger_shutdown("reason", "details")

    assert calls == []


class TestTriggerShutdownUnderOptimizedMode:
  def test_alerts_and_requests_a_fatal_shutdown_by_default(self) -> None:
    result, _ = _run_optimized("trigger_shutdown_alerts_and_requests_a_shutdown")

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
    result, _ = _run_optimized("alert_exception_sends_without_requesting_shutdown")

    assert result == {"alert_calls": 1, "shutdown_kind": "RUNNING"}

  def test_alert_email_content_includes_the_exception_and_label(self) -> None:
    result, _ = _run_optimized("alert_exception_content_includes_exception_and_label")

    # "Alert: [tests] " is the standardized prefix alert() adds to every
    # subject (see its docstring); "tests" comes from _resolve_program_name's
    # fallback to the entrypoint package's directory name, which for these
    # subprocess scenarios is always this repo's `tests/` package.
    assert result["subject"] == "Alert: [tests] Non-fatal exception in my.custom.label"
    assert result["mentions_exception"] is True

  def test_does_not_catch_or_affect_the_active_exception(self) -> None:
    result, _ = _run_optimized("alert_exception_does_not_affect_control_flow")

    assert result == {"propagated": True, "alert_calls": 1}


class TestPushAlertPriorityUnderOptimizedMode:
  def test_fatal_exception_uses_high_push_priority(self) -> None:
    result, _ = _run_optimized("fatal_exception_sends_push_alert_with_high_priority")

    assert result == {"push_alert_calls": 1, "priority": err_handling._FATAL_PUSH_PRIORITY}  # pyright: ignore[reportPrivateUsage]

  def test_alert_exception_uses_the_default_normal_push_priority(self) -> None:
    result, _ = _run_optimized("alert_exception_sends_push_alert_with_normal_priority")

    assert result == {"push_alert_calls": 1, "priority": 0}


class TestReportExcAlertsAndRequestsFatalShutdownUnderOptimizedMode:
  def test_alerts_and_requests_a_fatal_shutdown(self) -> None:
    result, _ = _run_optimized("report_exc_alerts_and_requests_fatal_shutdown")

    assert result == {
      "alert_calls": 1,
      "push_alert_calls": 1,
      "priority": err_handling._FATAL_PUSH_PRIORITY,  # pyright: ignore[reportPrivateUsage]
      "mentions_reason": True,
      "shutdown_kind": "FATAL",
    }

  def test_sets_the_current_fatal_trail(self) -> None:
    result, _ = _run_optimized("report_exc_sets_the_current_fatal_trail")

    # Run as a script (`python -O script.py`), so the raising frame's own module is "__main__".
    assert result == {"trail_count": 1, "origin_module": "__main__"}

  def test_still_shuts_down_when_trail_building_fails(self) -> None:
    result, _ = _run_optimized("report_exc_still_shuts_down_when_trail_building_fails")

    assert result == {"shutdown_kind": "FATAL", "trail_count": 0}


class TestCrashHooksAreNoOpsUnderNormalDebugMode:
  def test_install_crash_hooks_leaves_both_hooks_alone(self) -> None:
    before = (sys.excepthook, threading.excepthook)

    crash_hooks.install_crash_hooks()

    assert (sys.excepthook, threading.excepthook) == before

  def test_install_loop_exception_handler_leaves_the_loop_alone(self) -> None:
    async def main() -> object:
      crash_hooks.install_loop_exception_handler()
      return asyncio.get_running_loop().get_exception_handler()

    assert asyncio.run(main()) is None


class TestCrashHooksUnderOptimizedMode:
  def test_uncaught_main_thread_exception_alerts_and_shuts_down(self) -> None:
    result, stderr = _run_optimized("uncaught_main_thread_exception_alerts_and_shuts_down")

    assert result == {
      "alert_calls": 1,
      "push_alert_calls": 1,
      "labelled": True,
      "rendered_from_the_exception": True,
      "shutdown_kind": "FATAL",
    }
    # The shutdown pass narrates itself on fd 2 by design; the hook itself writes nothing there.
    assert "Traceback" not in stderr
    assert "main thread boom" not in stderr

  def test_uncaught_thread_exception_alerts_and_shuts_down(self) -> None:
    result, stderr = _run_optimized("uncaught_thread_exception_alerts_and_shuts_down")

    assert result == {
      "alert_calls": 1,
      "push_alert_calls": 1,
      "labelled": True,
      "rendered_from_the_exception": True,
      "shutdown_kind": "FATAL",
    }
    assert "Traceback" not in stderr
    assert "thread boom" not in stderr

  def test_thread_system_exit_is_ignored(self) -> None:
    result, _ = _run_optimized("thread_system_exit_is_ignored")

    assert result == {"alert_calls": 0, "logged": 0, "shutdown_set": False}

  def test_keyboard_interrupt_is_silent(self) -> None:
    result, _ = _run_optimized("keyboard_interrupt_is_silent")

    assert result == {"alert_calls": 0, "logged": 0, "shutdown_set": False}

  def test_hooks_are_quiet_once_shutdown_started(self) -> None:
    result, _ = _run_optimized("hooks_are_quiet_once_shutdown_started")

    assert result == {"alert_calls": 0, "push_alert_calls": 0, "shutdown_kind": "GRACEFUL"}

  def test_loop_exception_context_is_fatal(self) -> None:
    result, _ = _run_optimized("loop_exception_context_is_fatal")

    assert result == {
      "alert_calls": 1,
      "push_alert_calls": 1,
      "labelled": True,
      "rendered_from_the_exception": True,
      "shutdown_kind": "FATAL",
    }

  def test_fatal_shutdown_starts_even_when_reporting_fails(self) -> None:
    result, _ = _run_optimized("fatal_shutdown_starts_even_when_reporting_fails")

    assert result == {"escaped": False, "alert_calls": 0, "diagnosed_the_failure": True, "shutdown_kind": "FATAL"}

  def test_loop_message_only_context_logs_an_error(self) -> None:
    result, _ = _run_optimized("loop_message_only_context_logs_an_error")

    assert result == {"alert_calls": 0, "logged_error": True, "shutdown_set": False}


class TestAlertSendDeadline:
  @pytest.fixture
  def hanging_email(self, monkeypatch: pytest.MonkeyPatch) -> Generator[list[str]]:
    """Make the e-mail sender block until teardown and the push sender return at once; yield the threads each ran on."""
    release = threading.Event()
    calls: list[str] = []

    def hang(subject: str, content: str, *, image: bytes | None = None) -> None:
      calls.append(threading.current_thread().name)
      release.wait()

    monkeypatch.setattr(err_handling.SETTINGS, "alerts_send_timeout_secs", _SEND_TIMEOUT_SECS)
    monkeypatch.setattr(err_handling, "send_alert_email", hang)
    monkeypatch.setattr(err_handling, "send_alert_push", lambda *a, **k: calls.append(threading.current_thread().name))
    yield calls
    release.set()  # let the abandoned send thread exit instead of lingering until interpreter exit

  def test_alert_returns_by_the_deadline_when_a_sender_hangs(self, hanging_email: list[str]) -> None:
    start = time.monotonic()
    err_handling.alert("r", "d", in_except_block=False, force=True)

    assert time.monotonic() - start < _SEND_TIMEOUT_SECS * 2 + 1 + 0.5
    assert hanging_email.count("aeth-ext-alert-email") == 1

  def test_alert_push_still_sends_when_email_hangs(self, hanging_email: list[str]) -> None:
    start = time.monotonic()
    err_handling.alert("r", "d", in_except_block=False, force=True)

    assert time.monotonic() - start < _SEND_TIMEOUT_SECS * 2 + 1 + 0.5
    assert hanging_email.count("aeth-ext-alert-push") == 1

  def test_alert_wait_false_returns_immediately(self, hanging_email: list[str]) -> None:
    start = time.monotonic()
    err_handling.alert("r", "d", in_except_block=False, force=True, wait=False)

    assert time.monotonic() - start < _SEND_TIMEOUT_SECS
    deadline = time.monotonic() + 2
    while len(hanging_email) < 2 and time.monotonic() < deadline:  # noqa: PLR2004 -- one call per channel
      time.sleep(0.01)
    assert sorted(hanging_email) == ["aeth-ext-alert-email", "aeth-ext-alert-push"]

  def test_alert_still_sends_both_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
    emails: list[tuple[str, str]] = []
    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr(
      err_handling,
      "send_alert_email",
      lambda subject, content, *, image=None: emails.append((subject, threading.current_thread().name)),
    )
    monkeypatch.setattr(
      err_handling,
      "send_alert_push",
      lambda subject, content, *, priority=0, image=None: pushes.append((subject, threading.current_thread().name)),
    )

    err_handling.alert("r", "d", in_except_block=False, force=True)

    ((email_subject, email_thread),) = emails
    ((push_subject, push_thread),) = pushes
    assert email_subject == push_subject
    assert (email_thread, push_thread) == ("aeth-ext-alert-email", "aeth-ext-alert-push")


class TestInstanceSuffix:
  @pytest.fixture
  def uncached_program_name(self) -> Generator[None]:
    """Clear the cached program name before and after, so neither the test nor its neighbours see a stale one."""
    err_handling._resolve_program_name.cache_clear()  # pyright: ignore[reportPrivateUsage]
    yield
    err_handling._resolve_program_name.cache_clear()  # pyright: ignore[reportPrivateUsage]

  @pytest.mark.usefixtures("uncached_program_name")
  def test_alert_subject_carries_the_instance_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(err_handling.SETTINGS, "instance_id", None)
    project = err_handling._resolve_program_name()  # pyright: ignore[reportPrivateUsage]
    err_handling._resolve_program_name.cache_clear()  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(err_handling.SETTINGS, "instance_id", "3")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(err_handling, "send_alert_email", lambda subject, content, *, image=None: sent.append((subject, content)))
    monkeypatch.setattr(err_handling, "send_alert_push", lambda *a, **k: None)

    err_handling.alert("r", "d", in_except_block=False, force=True)

    ((subject, body),) = sent
    assert f"[{project}-3]" in subject
    assert f"Program: {project}-3\n" in body
