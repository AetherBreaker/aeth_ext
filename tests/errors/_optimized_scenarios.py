# Standard library imports
import asyncio
import json
import logging
import sys
import threading
import time
from asyncio import CancelledError
from logging.handlers import BufferingHandler

# First party imports
from aeth_ext.errors import crash_hooks, err_handling, shutdown
from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind
from aeth_ext.logging import emergency_diagnostics

assert not __debug__, "this harness must run under python -O"

alert_calls: list[tuple[str, str]] = []
err_handling.send_alert_email = lambda subject, content, *, image=None: alert_calls.append((subject, content))

push_alert_calls: list[tuple[str, str, int]] = []
err_handling.send_alert_push = lambda subject, content, *, priority=0, image=None: push_alert_calls.append(
  (subject, content, priority)
)

# Image rendering is exercised separately in test_traceback_image.py; stubbed
# here so these alert-count/priority scenarios don't pay for a real render.
# Records what each render was asked for, so a scenario can check the exception was passed through.
rendered_from: list[BaseException | None] = []
err_handling.render_exception_image = lambda exc=None: rendered_from.append(exc)

# Without a handler, logging's lastResort prints every record (traceback included) to stderr, which would hide whether
# the crash hooks write there themselves. Keeping the records also lets scenarios check what was logged.
log_records = BufferingHandler(capacity=sys.maxsize)
logging.getLogger().addHandler(log_records)

# Recorded rather than written, so no scenario appends to the real persisted diagnostics file.
diagnostics: list[tuple[str, BaseException | None]] = []
emergency_diagnostics.emergency_diagnostic = lambda message, *, exc=None: diagnostics.append((message, exc))


def handle_fatal_exc_sync_generic_exception() -> dict[str, object]:
  @err_handling.handle_fatal_exc_sync
  def func() -> None:
    raise ValueError("boom")

  # run_shutdown() now unconditionally sends an exit nudge that can race this
  # call; see report_exc_alerts_and_requests_fatal_shutdown's docstring for
  # why swallowing the one-shot KeyboardInterrupt here loses no information.
  try:
    returned = func()
  except KeyboardInterrupt:
    returned = None
  return {
    "returned": returned,
    "alert_calls": len(alert_calls),
    "push_alert_calls": len(push_alert_calls),
    "shutdown_kind": SHUTDOWN.kind.name,
  }


def handle_fatal_exc_sync_cancelled_error() -> dict[str, object]:
  @err_handling.handle_fatal_exc_sync
  def func() -> None:
    raise CancelledError

  try:
    func()
    propagated = False
  except CancelledError:
    propagated = True
  return {"propagated": propagated, "alert_calls": len(alert_calls)}


def handle_fatal_exc_async_generic_exception() -> dict[str, object]:
  @err_handling.handle_fatal_exc_async
  async def func() -> None:
    raise ValueError("boom")

  # See report_exc_alerts_and_requests_fatal_shutdown's docstring: the
  # unconditional exit nudge races this call, and the one-shot
  # KeyboardInterrupt is safe to swallow since every side effect checked
  # below is already synchronous by the time it can land.
  try:
    returned = asyncio.run(func())
  except KeyboardInterrupt:
    returned = None
  return {"returned": returned, "alert_calls": len(alert_calls), "shutdown_kind": SHUTDOWN.kind.name}


def handle_fatal_exc_async_cancelled_error() -> dict[str, object]:
  @err_handling.handle_fatal_exc_async
  async def func() -> None:
    raise CancelledError

  try:
    asyncio.run(func())
    propagated = False
  except CancelledError:
    propagated = True
  return {"propagated": propagated, "alert_calls": len(alert_calls)}


def handle_fatal_exc_async_generator_exit() -> dict[str, object]:
  @err_handling.handle_fatal_exc_async
  async def func() -> None:
    raise GeneratorExit

  returned = asyncio.run(func())
  return {"returned": returned, "alert_calls": len(alert_calls), "shutdown_kind": SHUTDOWN.kind.name}


def report_exc_default_swallows() -> dict[str, object]:
  # typeshed types contextlib.contextmanager's __exit__ as never suppressing,
  # so pyright can't see that report_exc's generator actually swallows here
  # (reraise=False, the default) -- this line is genuinely reached at runtime.
  # See report_exc_alerts_and_requests_fatal_shutdown's docstring: the
  # unconditional exit nudge races this call, and the one-shot
  # KeyboardInterrupt is safe to swallow since every side effect checked
  # below is already synchronous by the time it can land.
  try:
    with err_handling.report_exc("label"):
      raise ValueError("boom")
  except KeyboardInterrupt:
    pass
  return {"alert_calls": len(alert_calls), "shutdown_kind": SHUTDOWN.kind.name}


def report_exc_reraise_true_propagates() -> dict[str, object]:
  try:
    with err_handling.report_exc("label", reraise=True):
      raise ValueError("boom")
    propagated = False  # pyright: ignore[reportUnreachable]  # reraise=True: this line never actually runs
  # The unconditional exit nudge can beat the reraised ValueError to this
  # `except` clause if it lands first -- either way something propagated out
  # of the `with` block, which is the thing under test, and alert_calls is
  # already recorded by the time _handle_fatal runs, so this loses no
  # information (see report_exc_alerts_and_requests_fatal_shutdown's
  # docstring for the general reasoning).
  except ValueError, KeyboardInterrupt:
    propagated = True
  return {"propagated": propagated, "alert_calls": len(alert_calls)}


def report_exc_cancelled_error_always_propagates() -> dict[str, object]:
  try:
    with err_handling.report_exc("label"):
      raise CancelledError
    propagated = False  # pyright: ignore[reportUnreachable]  # CancelledError always re-raises: this line never actually runs
  except CancelledError:
    propagated = True
  return {"propagated": propagated, "alert_calls": len(alert_calls)}


def alert_exception_sends_without_requesting_shutdown() -> dict[str, object]:
  try:
    raise ValueError("boom")
  except ValueError as e:
    err_handling.alert_exception("some.label", e)
  return {"alert_calls": len(alert_calls), "shutdown_kind": SHUTDOWN.kind.name}


def alert_exception_content_includes_exception_and_label() -> dict[str, object]:
  try:
    raise ValueError("very specific message")
  except ValueError as e:
    err_handling.alert_exception("my.custom.label", e)
  subject, content = alert_calls[0]
  return {"subject": subject, "mentions_exception": "very specific message" in content}


def alert_exception_does_not_affect_control_flow() -> dict[str, object]:
  propagated = False
  try:
    try:
      raise ValueError("boom")
    except ValueError as e:
      err_handling.alert_exception("some.label", e)
      raise
  except ValueError:
    propagated = True
  return {"propagated": propagated, "alert_calls": len(alert_calls)}


def fatal_exception_sends_push_alert_with_high_priority() -> dict[str, object]:
  @err_handling.handle_fatal_exc_sync
  def func() -> None:
    raise ValueError("boom")

  # See report_exc_alerts_and_requests_fatal_shutdown's docstring: the
  # unconditional exit nudge races this call, and the one-shot
  # KeyboardInterrupt is safe to swallow since push_alert_calls is already
  # populated.
  try:
    func()
  except KeyboardInterrupt:
    pass
  _subject, _content, priority = push_alert_calls[-1]
  return {"push_alert_calls": len(push_alert_calls), "priority": priority}


def alert_exception_sends_push_alert_with_normal_priority() -> dict[str, object]:
  try:
    raise ValueError("boom")
  except ValueError as e:
    err_handling.alert_exception("some.label", e)
  _subject, _content, priority = push_alert_calls[-1]
  return {"push_alert_calls": len(push_alert_calls), "priority": priority}


def report_exc_alerts_and_requests_fatal_shutdown() -> dict[str, object]:
  try:
    with err_handling.report_exc("label"):
      raise ValueError("remote logging config rejected: bad handler")
  except KeyboardInterrupt:
    pass
  _subject, content, priority = push_alert_calls[-1]
  return {
    "alert_calls": len(alert_calls),
    "push_alert_calls": len(push_alert_calls),
    "priority": priority,
    "mentions_reason": "bad handler" in content,
    "shutdown_kind": SHUTDOWN.kind.name,
  }


def report_exc_sets_the_current_fatal_trail() -> dict[str, object]:
  try:
    with err_handling.report_exc("label"):
      raise ValueError("boom")
  except KeyboardInterrupt:
    pass
  trails = shutdown.get_current_fatal_trails()
  return {"trail_count": len(trails), "origin_module": trails[0].origin.module if trails else None}


def _raise_instead_of_building_a_trail(exc: BaseException, *, walk_chain: bool = True) -> object:
  raise RuntimeError("trail construction boom")


def report_exc_still_shuts_down_when_trail_building_fails() -> dict[str, object]:
  err_handling.build_exception_trail = _raise_instead_of_building_a_trail
  try:
    with err_handling.report_exc("label"):
      raise ValueError("boom")
  except KeyboardInterrupt:
    pass
  return {
    "shutdown_kind": SHUTDOWN.kind.name,
    "trail_count": len(shutdown.get_current_fatal_trails()),
    "diagnosed_the_failure": [str(exc) for _message, exc in diagnostics] == ["trail construction boom"],
  }


class _TrailInterrupt(KeyboardInterrupt):
  """Told apart from the shutdown's own exit nudge, which is a plain KeyboardInterrupt."""


def _interrupt_instead_of_building_a_trail(exc: BaseException, *, walk_chain: bool = True) -> object:
  raise _TrailInterrupt


def interrupt_during_the_trail_unwinds_after_the_shutdown_starts() -> dict[str, object]:
  err_handling.build_exception_trail = _interrupt_instead_of_building_a_trail
  try:
    with err_handling.report_exc("label"):
      raise ValueError("boom")
    propagated = False  # pyright: ignore[reportUnreachable]  # report_exc swallows by default; reached if the interrupt is eaten
  except _TrailInterrupt:
    propagated = True
  except KeyboardInterrupt:  # the exit nudge
    propagated = False
  return {"propagated": propagated, "diagnostics": len(diagnostics), "shutdown_kind": SHUTDOWN.kind.name}


def trigger_shutdown_alerts_and_requests_a_shutdown() -> dict[str, object]:
  try:
    err_handling.trigger_shutdown("Central log server rejected logging config for 'prog'", "bad handler")
  except KeyboardInterrupt:
    pass
  _subject, content, priority = push_alert_calls[-1]
  return {
    "alert_calls": len(alert_calls),
    "push_alert_calls": len(push_alert_calls),
    "priority": priority,
    "mentions_reason": "bad handler" in content,
    "shutdown_kind": SHUTDOWN.kind.name,
  }


def _raise_in_thread(exc: BaseException) -> None:
  def target() -> None:
    raise exc

  worker = threading.Thread(target=target, name="crash-worker")
  worker.start()
  worker.join()


def uncaught_main_thread_exception_alerts_and_shuts_down() -> dict[str, object]:
  crash_hooks.install_crash_hooks()
  try:
    raise ValueError("main thread boom")
  except ValueError as e:
    caught = e
  # Outside the except block sys.exc_info() is empty, as it is when the interpreter calls the hook.
  try:
    sys.excepthook(type(caught), caught, caught.__traceback__)
  except KeyboardInterrupt:  # the exit nudge; see report_exc_alerts_and_requests_fatal_shutdown
    pass
  subject, _content = alert_calls[-1]
  return {
    "alert_calls": len(alert_calls),
    "push_alert_calls": len(push_alert_calls),
    "labelled": "uncaught exception in the main thread" in subject,
    "rendered_from_the_exception": rendered_from == [caught],
    "shutdown_kind": SHUTDOWN.kind.name,
  }


def uncaught_thread_exception_alerts_and_shuts_down() -> dict[str, object]:
  crash_hooks.install_crash_hooks()
  exc = ValueError("thread boom")
  try:
    _raise_in_thread(exc)
  except KeyboardInterrupt:
    pass
  subject, _content = alert_calls[-1]
  return {
    "alert_calls": len(alert_calls),
    "push_alert_calls": len(push_alert_calls),
    "labelled": "uncaught exception in thread crash-worker" in subject,
    "rendered_from_the_exception": rendered_from == [exc],
    "shutdown_kind": SHUTDOWN.kind.name,
  }


def thread_system_exit_is_ignored() -> dict[str, object]:
  crash_hooks.install_crash_hooks()
  _raise_in_thread(SystemExit(3))
  return {"alert_calls": len(alert_calls), "logged": len(log_records.buffer), "shutdown_set": SHUTDOWN.is_set()}


def keyboard_interrupt_is_silent() -> dict[str, object]:
  crash_hooks.install_crash_hooks()
  try:
    raise KeyboardInterrupt
  except KeyboardInterrupt as e:
    caught = e
  sys.excepthook(type(caught), caught, caught.__traceback__)
  _raise_in_thread(KeyboardInterrupt())
  return {"alert_calls": len(alert_calls), "logged": len(log_records.buffer), "shutdown_set": SHUTDOWN.is_set()}


def hooks_are_quiet_once_shutdown_started() -> dict[str, object]:
  crash_hooks.install_crash_hooks()
  # Take the pass's one-shot exit nudge here, so it can't land in (and cut short) the steps under test. Short
  # sleeps, since on Windows interrupt_main() can't cut a sleep short.
  try:
    shutdown.run_shutdown(ShutdownKind.GRACEFUL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
      time.sleep(0.01)
  except KeyboardInterrupt:
    pass
  try:
    raise ValueError("main thread boom")
  except ValueError as e:
    caught = e
  sys.excepthook(type(caught), caught, caught.__traceback__)
  _raise_in_thread(ValueError("thread boom"))
  return {"alert_calls": len(alert_calls), "push_alert_calls": len(push_alert_calls), "shutdown_kind": SHUTDOWN.kind.name}


def loop_exception_context_is_fatal() -> dict[str, object]:
  exc = ValueError("loop boom")

  async def main() -> None:
    crash_hooks.install_loop_exception_handler()
    asyncio.get_running_loop().call_exception_handler({"message": "Task exception was never retrieved", "exception": exc})

  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    pass
  subject, _content = alert_calls[-1]
  return {
    "alert_calls": len(alert_calls),
    "push_alert_calls": len(push_alert_calls),
    "labelled": "unhandled exception in the event loop: Task exception was never retrieved" in subject,
    "rendered_from_the_exception": rendered_from == [exc],
    "shutdown_kind": SHUTDOWN.kind.name,
  }


def loop_message_only_context_logs_an_error() -> dict[str, object]:
  async def main() -> None:
    crash_hooks.install_loop_exception_handler()
    asyncio.get_running_loop().call_exception_handler({"message": "Task was destroyed but it is pending!"})

  asyncio.run(main())
  return {
    "alert_calls": len(alert_calls),
    "logged_error": any(
      r.levelno == logging.ERROR and "Task was destroyed but it is pending!" in r.getMessage() for r in log_records.buffer
    ),
    "shutdown_set": SHUTDOWN.is_set(),
  }


def _raise_instead_of_rendering(exc: BaseException) -> str:
  raise RuntimeError("render boom")


def fatal_shutdown_starts_even_when_reporting_fails() -> dict[str, object]:
  err_handling._extract_rich_traceback = _raise_instead_of_rendering  # pyright: ignore[reportPrivateUsage]
  crash_hooks.install_crash_hooks()
  try:
    raise ValueError("main thread boom")
  except ValueError as e:
    caught = e
  try:
    sys.excepthook(type(caught), caught, caught.__traceback__)
    escaped = False
  except RuntimeError:
    escaped = True
  except KeyboardInterrupt:
    escaped = False
  return {
    "escaped": escaped,
    "alert_calls": len(alert_calls),
    "diagnosed_the_failure": [str(exc) for _message, exc in diagnostics] == ["render boom"],
    "shutdown_kind": SHUTDOWN.kind.name,
  }


_SCENARIOS = {
  "handle_fatal_exc_sync_generic_exception": handle_fatal_exc_sync_generic_exception,
  "handle_fatal_exc_sync_cancelled_error": handle_fatal_exc_sync_cancelled_error,
  "handle_fatal_exc_async_generic_exception": handle_fatal_exc_async_generic_exception,
  "handle_fatal_exc_async_cancelled_error": handle_fatal_exc_async_cancelled_error,
  "handle_fatal_exc_async_generator_exit": handle_fatal_exc_async_generator_exit,
  "report_exc_default_swallows": report_exc_default_swallows,
  "report_exc_reraise_true_propagates": report_exc_reraise_true_propagates,
  "report_exc_cancelled_error_always_propagates": report_exc_cancelled_error_always_propagates,
  "alert_exception_sends_without_requesting_shutdown": alert_exception_sends_without_requesting_shutdown,
  "alert_exception_content_includes_exception_and_label": alert_exception_content_includes_exception_and_label,
  "alert_exception_does_not_affect_control_flow": alert_exception_does_not_affect_control_flow,
  "fatal_exception_sends_push_alert_with_high_priority": fatal_exception_sends_push_alert_with_high_priority,
  "alert_exception_sends_push_alert_with_normal_priority": alert_exception_sends_push_alert_with_normal_priority,
  "report_exc_alerts_and_requests_fatal_shutdown": report_exc_alerts_and_requests_fatal_shutdown,
  "report_exc_sets_the_current_fatal_trail": report_exc_sets_the_current_fatal_trail,
  "report_exc_still_shuts_down_when_trail_building_fails": report_exc_still_shuts_down_when_trail_building_fails,
  "interrupt_during_the_trail_unwinds_after_the_shutdown_starts": interrupt_during_the_trail_unwinds_after_the_shutdown_starts,
  "trigger_shutdown_alerts_and_requests_a_shutdown": trigger_shutdown_alerts_and_requests_a_shutdown,
  "uncaught_main_thread_exception_alerts_and_shuts_down": uncaught_main_thread_exception_alerts_and_shuts_down,
  "uncaught_thread_exception_alerts_and_shuts_down": uncaught_thread_exception_alerts_and_shuts_down,
  "thread_system_exit_is_ignored": thread_system_exit_is_ignored,
  "keyboard_interrupt_is_silent": keyboard_interrupt_is_silent,
  "hooks_are_quiet_once_shutdown_started": hooks_are_quiet_once_shutdown_started,
  "loop_exception_context_is_fatal": loop_exception_context_is_fatal,
  "loop_message_only_context_logs_an_error": loop_message_only_context_logs_an_error,
  "fatal_shutdown_starts_even_when_reporting_fails": fatal_shutdown_starts_even_when_reporting_fails,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps(result))
