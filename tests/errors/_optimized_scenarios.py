"""Real, importable scenario functions for `-O` (`__debug__ == False`) subprocess tests.

Written as genuine Python source -- not string-embedded code passed to
`python -c` -- so IDE rename-symbol tooling can track and update references
to the symbols under test here exactly like any other file in the suite.
Each scenario is selected by name via `argv[1]` and this module is run in a
fresh `-O` interpreter by `_run_optimized` in `test_err_handling.py`, since
`__debug__` cannot be flipped at runtime and `SHUTDOWN` is a one-shot,
process-wide state that must not be tripped in the main pytest process.
"""

# Standard library imports
import asyncio
import json
import sys
from asyncio import CancelledError

# First party imports
from aeth_ext.errors import err_handling, shutdown
from aeth_ext.errors.shutdown import SHUTDOWN

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
  """`report_exc` alerts and drives `run_shutdown(FATAL)`, which always nudges the main thread (D-I3/D-I4).

  The shutdown thread nudges this process's main thread with
  `_thread.interrupt_main()` once its threaded pass finishes, unconditionally
  -- which, in this fresh subprocess with nothing registered, happens almost
  immediately and races the rest of this function. Every side effect checked
  below (`alert_calls`, `push_alert_calls`, `SHUTDOWN.kind`) is already
  synchronous by the time the `with` block exits or the interrupt lands, so
  catching the one-shot `KeyboardInterrupt` here loses no information and
  never needs a retry.
  """
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
  """D-copilot-F: `_handle_fatal` calls `_set_current_fatal_trail` before `run_shutdown(FATAL)`,
  but no scenario had driven that call through the real `report_exc`/`_handle_fatal` path rather
  than poking the private setter directly -- a refactor that dropped the call would pass the
  whole suite. See `report_exc_alerts_and_requests_fatal_shutdown`'s docstring for the
  `KeyboardInterrupt` swallow."""
  try:
    with err_handling.report_exc("label"):
      raise ValueError("boom")
  except KeyboardInterrupt:
    pass
  trails = shutdown.get_current_fatal_trails()
  return {"trail_count": len(trails), "origin_module": trails[0].origin.module if trails else None}


def trigger_shutdown_alerts_and_requests_a_shutdown() -> dict[str, object]:
  """`trigger_shutdown` alerts and drives `run_shutdown` with no synthetic exception involved."""
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
  "trigger_shutdown_alerts_and_requests_a_shutdown": trigger_shutdown_alerts_and_requests_a_shutdown,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps(result))
