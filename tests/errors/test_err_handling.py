"""Tests for `aeth_ext.errors.err_handling`.

`handle_fatal_exc_sync`/`handle_fatal_exc_async`/`report_exc` are no-ops under
`__debug__ == True` (the default for a normal, non-optimized interpreter --
i.e. every in-process pytest run). Since `__debug__` cannot be flipped at
runtime, the "actually wraps and alerts" behavior can only be exercised in a
fresh interpreter started with `-O`. That happens to be convenient here for
another reason too: `FATAL_EVENT` is a one-shot `aiologic.Event` that can
never be un-set, so triggering it in-process would permanently poison the
rest of this pytest session. Running each such scenario in its own
`-O` subprocess keeps both concerns isolated.
"""

# Standard library imports
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.errors import err_handling

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping


def _run_optimized(body: str) -> Mapping[str, object]:
  """Run `body` in a fresh `-O` (i.e. `__debug__ == False`) subprocess.

  `body` must set a `result` dict and end by printing it as JSON -- see
  `_HARNESS` for the shared setup every scenario is appended to.
  """
  script = _HARNESS + body
  env = dict(os.environ)
  env.setdefault("ALERTS_EMAIL_PWD", "test-password")

  proc = subprocess.run(
    [sys.executable, "-O", "-c", script],
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
    check=False,
  )

  assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

  return json.loads(proc.stdout.strip().splitlines()[-1])


_HARNESS = """
import asyncio, json
from aeth_ext.errors import err_handling

assert not __debug__, "this harness must run under python -O"

alert_calls = []
err_handling.send_alert_email = lambda subject, content: alert_calls.append((subject, content))

"""


class TestNoOpUnderNormalDebugMode:
  """Under a normal (non `-O`) interpreter, all three are true pass-throughs."""

  def test_handle_fatal_exc_sync_bare_returns_the_original_function(self):
    def func() -> None:
      pass

    assert err_handling.handle_fatal_exc_sync(func) is func

  def test_handle_fatal_exc_sync_with_extract_details_returns_the_original_function(self):
    def func() -> None:
      pass

    decorator = err_handling.handle_fatal_exc_sync(extract_details_callable=lambda e: None)
    assert decorator(func) is func

  def test_handle_fatal_exc_async_bare_returns_the_original_function(self):
    async def func() -> None:
      pass

    assert err_handling.handle_fatal_exc_async(func) is func

  def test_report_exc_lets_exceptions_propagate_unmodified(self):
    with pytest.raises(ValueError, match="boom"), err_handling.report_exc("label"):
      raise ValueError("boom")

  def test_report_exc_no_exception_is_a_no_op(self):
    with err_handling.report_exc("label"):
      pass


class TestHandleFatalExcSyncUnderOptimizedMode:
  def test_generic_exception_is_alerted_and_swallowed(self):
    result = _run_optimized("""
@err_handling.handle_fatal_exc_sync
def func():
  raise ValueError("boom")

ret = func()
print(json.dumps({
  "returned": ret,
  "alert_calls": len(alert_calls),
  "fatal_event_set": err_handling.FATAL_EVENT.is_set(),
}))
""")

    assert result == {"returned": None, "alert_calls": 1, "fatal_event_set": True}

  def test_cancelled_error_propagates_without_alerting(self):
    result = _run_optimized("""
from asyncio import CancelledError

@err_handling.handle_fatal_exc_sync
def func():
  raise CancelledError()

try:
  func()
  propagated = False
except CancelledError:
  propagated = True

print(json.dumps({"propagated": propagated, "alert_calls": len(alert_calls)}))
""")

    assert result == {"propagated": True, "alert_calls": 0}

  def test_extract_details_callable_is_invoked_with_the_exception(self):
    result = _run_optimized("""
seen = []

@err_handling.handle_fatal_exc_sync(extract_details_callable=lambda e: seen.append(str(e)))
def func():
  raise ValueError("details please")

func()
print(json.dumps({"seen": seen, "alert_calls": len(alert_calls)}))
""")

    assert result == {"seen": ["details please"], "alert_calls": 1}

  def test_extract_details_callable_failure_is_caught_not_propagated(self):
    result = _run_optimized("""
@err_handling.handle_fatal_exc_sync(extract_details_callable=lambda e: 1 / 0)
def func():
  raise ValueError("boom")

ret = func()
print(json.dumps({"returned": ret, "alert_calls": len(alert_calls)}))
""")

    assert result == {"returned": None, "alert_calls": 1}


class TestHandleFatalExcAsyncUnderOptimizedMode:
  def test_generic_exception_is_alerted_and_swallowed(self):
    result = _run_optimized("""
@err_handling.handle_fatal_exc_async
async def func():
  raise ValueError("boom")

ret = asyncio.run(func())
print(json.dumps({
  "returned": ret,
  "alert_calls": len(alert_calls),
  "fatal_event_set": err_handling.FATAL_EVENT.is_set(),
}))
""")

    assert result == {"returned": None, "alert_calls": 1, "fatal_event_set": True}

  def test_cancelled_error_propagates_without_alerting(self):
    result = _run_optimized("""
from asyncio import CancelledError

@err_handling.handle_fatal_exc_async
async def func():
  raise CancelledError()

try:
  asyncio.run(func())
  propagated = False
except CancelledError:
  propagated = True

print(json.dumps({"propagated": propagated, "alert_calls": len(alert_calls)}))
""")

    assert result == {"propagated": True, "alert_calls": 0}

  def test_generator_exit_returns_none_without_alerting(self):
    result = _run_optimized("""
@err_handling.handle_fatal_exc_async
async def func():
  raise GeneratorExit()

ret = asyncio.run(func())
print(json.dumps({
  "returned": ret,
  "alert_calls": len(alert_calls),
  "fatal_event_set": err_handling.FATAL_EVENT.is_set(),
}))
""")

    assert result == {"returned": None, "alert_calls": 0, "fatal_event_set": False}

  def test_extract_details_callable_is_invoked_with_the_exception(self):
    result = _run_optimized("""
seen = []

@err_handling.handle_fatal_exc_async(extract_details_callable=lambda e: seen.append(str(e)))
async def func():
  raise ValueError("details please")

asyncio.run(func())
print(json.dumps({"seen": seen, "alert_calls": len(alert_calls)}))
""")

    assert result == {"seen": ["details please"], "alert_calls": 1}


class TestReportExcUnderOptimizedMode:
  def test_default_swallows_the_exception_after_alerting(self):
    result = _run_optimized("""
with err_handling.report_exc("label"):
  raise ValueError("boom")

print(json.dumps({
  "alert_calls": len(alert_calls),
  "fatal_event_set": err_handling.FATAL_EVENT.is_set(),
}))
""")

    assert result == {"alert_calls": 1, "fatal_event_set": True}

  def test_reraise_true_propagates_after_alerting(self):
    result = _run_optimized("""
try:
  with err_handling.report_exc("label", reraise=True):
    raise ValueError("boom")
  propagated = False
except ValueError:
  propagated = True

print(json.dumps({"propagated": propagated, "alert_calls": len(alert_calls)}))
""")

    assert result == {"propagated": True, "alert_calls": 1}

  def test_cancelled_error_always_propagates_without_alerting(self):
    result = _run_optimized("""
from asyncio import CancelledError

try:
  with err_handling.report_exc("label"):
    raise CancelledError()
  propagated = False
except CancelledError:
  propagated = True

print(json.dumps({"propagated": propagated, "alert_calls": len(alert_calls)}))
""")

    assert result == {"propagated": True, "alert_calls": 0}
