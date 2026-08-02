"""Real, importable scenario functions for `-O` (`__debug__ == False`) subprocess tests.

Written as genuine Python source -- not string-embedded code passed to
`python -c` -- so IDE rename-symbol tooling can track and update references
to the symbols under test here exactly like any other file in the suite.
Each scenario is selected by name via `argv[1]` and this module is run in a
fresh `-O` interpreter by `_run_optimized` in `test_err_handling.py`, since
`__debug__` cannot be flipped at runtime and `FATAL_EVENT` is a one-shot
event that must not be tripped in the main pytest process.
"""

# Standard library imports
import asyncio
import json
import sys
from asyncio import CancelledError

# First party imports
from aeth_ext.errors import err_handling

assert not __debug__, "this harness must run under python -O"

alert_calls: list[tuple[str, str]] = []
err_handling.send_alert_email = lambda subject, content, *, image=None: alert_calls.append((subject, content))

push_alert_calls: list[tuple[str, str, int]] = []
err_handling.send_alert_push = lambda subject, content, *, priority=0, image=None: push_alert_calls.append(
  (subject, content, priority)
)

# Image rendering is exercised separately in test_traceback_image.py; stubbed
# here so these alert-count/priority scenarios don't pay for a real render.
err_handling.render_exception_image = lambda: None


def handle_fatal_exc_sync_generic_exception() -> dict[str, object]:
  @err_handling.handle_fatal_exc_sync
  def func() -> None:
    raise ValueError("boom")

  returned = func()
  return {
    "returned": returned,
    "alert_calls": len(alert_calls),
    "push_alert_calls": len(push_alert_calls),
    "fatal_event_set": err_handling.FATAL_EVENT.is_set(),
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


def handle_fatal_exc_sync_extract_details_callable_invoked() -> dict[str, object]:
  seen: list[str] = []

  @err_handling.handle_fatal_exc_sync(extract_details_callable=lambda e: seen.append(str(e)))
  def func() -> None:
    raise ValueError("details please")

  func()
  return {"seen": seen, "alert_calls": len(alert_calls)}


def handle_fatal_exc_sync_extract_details_callable_failure_is_caught() -> dict[str, object]:
  @err_handling.handle_fatal_exc_sync(extract_details_callable=lambda e: 1 / 0)
  def func() -> None:
    raise ValueError("boom")

  returned = func()
  return {"returned": returned, "alert_calls": len(alert_calls)}


def handle_fatal_exc_async_generic_exception() -> dict[str, object]:
  @err_handling.handle_fatal_exc_async
  async def func() -> None:
    raise ValueError("boom")

  returned = asyncio.run(func())
  return {"returned": returned, "alert_calls": len(alert_calls), "fatal_event_set": err_handling.FATAL_EVENT.is_set()}


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
  return {"returned": returned, "alert_calls": len(alert_calls), "fatal_event_set": err_handling.FATAL_EVENT.is_set()}


def handle_fatal_exc_async_extract_details_callable_invoked() -> dict[str, object]:
  seen: list[str] = []

  @err_handling.handle_fatal_exc_async(extract_details_callable=lambda e: seen.append(str(e)))
  async def func() -> None:
    raise ValueError("details please")

  asyncio.run(func())
  return {"seen": seen, "alert_calls": len(alert_calls)}


def report_exc_default_swallows() -> dict[str, object]:
  # typeshed types contextlib.contextmanager's __exit__ as never suppressing,
  # so pyright can't see that report_exc's generator actually swallows here
  # (reraise=False, the default) -- this line is genuinely reached at runtime.
  with err_handling.report_exc("label"):
    raise ValueError("boom")
  return {"alert_calls": len(alert_calls), "fatal_event_set": err_handling.FATAL_EVENT.is_set()}  # pyright: ignore[reportUnreachable]


def report_exc_reraise_true_propagates() -> dict[str, object]:
  try:
    with err_handling.report_exc("label", reraise=True):
      raise ValueError("boom")
    propagated = False  # pyright: ignore[reportUnreachable]  # reraise=True: this line never actually runs
  except ValueError:
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


def alert_exception_sends_without_fatal_event() -> dict[str, object]:
  try:
    raise ValueError("boom")
  except ValueError as e:
    err_handling.alert_exception("some.label", e)
  return {"alert_calls": len(alert_calls), "fatal_event_set": err_handling.FATAL_EVENT.is_set()}


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

  func()
  _subject, _content, priority = push_alert_calls[-1]
  return {"push_alert_calls": len(push_alert_calls), "priority": priority}


def alert_exception_sends_push_alert_with_normal_priority() -> dict[str, object]:
  try:
    raise ValueError("boom")
  except ValueError as e:
    err_handling.alert_exception("some.label", e)
  _subject, _content, priority = push_alert_calls[-1]
  return {"push_alert_calls": len(push_alert_calls), "priority": priority}


_SCENARIOS = {
  "handle_fatal_exc_sync_generic_exception": handle_fatal_exc_sync_generic_exception,
  "handle_fatal_exc_sync_cancelled_error": handle_fatal_exc_sync_cancelled_error,
  "handle_fatal_exc_sync_extract_details_callable_invoked": handle_fatal_exc_sync_extract_details_callable_invoked,
  "handle_fatal_exc_sync_extract_details_callable_failure_is_caught": (
    handle_fatal_exc_sync_extract_details_callable_failure_is_caught
  ),
  "handle_fatal_exc_async_generic_exception": handle_fatal_exc_async_generic_exception,
  "handle_fatal_exc_async_cancelled_error": handle_fatal_exc_async_cancelled_error,
  "handle_fatal_exc_async_generator_exit": handle_fatal_exc_async_generator_exit,
  "handle_fatal_exc_async_extract_details_callable_invoked": handle_fatal_exc_async_extract_details_callable_invoked,
  "report_exc_default_swallows": report_exc_default_swallows,
  "report_exc_reraise_true_propagates": report_exc_reraise_true_propagates,
  "report_exc_cancelled_error_always_propagates": report_exc_cancelled_error_always_propagates,
  "alert_exception_sends_without_fatal_event": alert_exception_sends_without_fatal_event,
  "alert_exception_content_includes_exception_and_label": alert_exception_content_includes_exception_and_label,
  "alert_exception_does_not_affect_control_flow": alert_exception_does_not_affect_control_flow,
  "fatal_exception_sends_push_alert_with_high_priority": fatal_exception_sends_push_alert_with_high_priority,
  "alert_exception_sends_push_alert_with_normal_priority": alert_exception_sends_push_alert_with_normal_priority,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps(result))
