"""Global crash hooks: under ``python -O``, an exception nothing caught is fatal through ``_handle_fatal``.

Covers the main thread (``sys.excepthook``), every other thread (``threading.excepthook``), and an asyncio
loop's exception handler. None of them chain to Python's default hooks or write to stderr: the failure is
logged through aeth_ext's logging and alerted instead.

Every hook stays quiet once ``SHUTDOWN`` is set. That also skips an exception a decorator already reported,
since ``_handle_fatal`` always starts the shutdown, so no per-exception marker is needed.
"""

# Standard library imports
import sys
import threading
from asyncio import get_running_loop
from logging import getLogger
from typing import TYPE_CHECKING, Any

# First party imports
from aeth_ext.errors.err_handling import _handle_fatal  # pyright: ignore[reportPrivateUsage]
from aeth_ext.errors.shutdown import SHUTDOWN

if TYPE_CHECKING:
  # Standard library imports
  from asyncio import AbstractEventLoop
  from types import TracebackType

logger = getLogger(__name__)

__all__ = ["install_crash_hooks", "install_loop_exception_handler"]


def _main_thread_excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
  # KeyboardInterrupt is aeth_ext's own exit nudge (or an operator's last-rung interrupt), not a crash.
  if isinstance(exc, KeyboardInterrupt) or SHUTDOWN.is_set():
    return
  _handle_fatal("the main thread", exc)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
  exc = args.exc_value
  # SystemExit ends a thread quietly, as the stdlib hook treats it; exc_value is None only for a hand-made call.
  if exc is None or isinstance(exc, KeyboardInterrupt | SystemExit) or SHUTDOWN.is_set():
    return
  name = args.thread.name if args.thread is not None else threading.current_thread().name
  _handle_fatal(f"thread {name}", exc)


def _loop_exception_handler(loop: AbstractEventLoop, context: dict[str, Any]) -> None:
  if SHUTDOWN.is_set():
    return
  message = context.get("message", "")
  exc = context.get("exception")
  if exc is None:
    # No exception to report (e.g. "Task was destroyed but it is pending!"): worth seeing, not worth dying for.
    logger.error("Event loop error: %s %r", message, {key: value for key, value in context.items() if key != "message"})
  else:
    _handle_fatal(f"the event loop: {message}", exc)


def install_crash_hooks() -> None:
  """Route uncaught exceptions in the main thread and in every other thread to a fatal shutdown.

  A no-op under ``__debug__``, like the rest of fatal handling, so development keeps Python's tracebacks.
  ``initialize`` calls this after logging setup, whose Rich ``install()`` replaces ``sys.excepthook``.
  """
  if __debug__:
    return
  sys.excepthook = _main_thread_excepthook
  threading.excepthook = _thread_excepthook


def install_loop_exception_handler() -> None:
  """Make an exception the running event loop reports fatal; log a report without one as an error.

  Call from inside the loop: it must be set on the running loop, since a server such as uvicorn creates its
  own. A no-op under ``__debug__``, except that calling it outside a loop raises there too, rather than only
  under ``-O``.

  Raises:
    RuntimeError: No event loop is running in this thread.
  """
  loop = get_running_loop()
  if __debug__:
    return
  loop.set_exception_handler(_loop_exception_handler)
