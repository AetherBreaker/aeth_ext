# Standard library imports
from asyncio import CancelledError
from contextlib import contextmanager
from datetime import datetime
from functools import cache, wraps
from io import StringIO
from logging import getLogger
from os.path import basename
from socket import gethostname
from typing import TYPE_CHECKING, overload

# Third party imports
from rich.console import Console

# First party imports
from aeth_ext.errors.send_alert_email import send_alert_email
from aeth_ext.errors.send_alert_push import send_alert_push
from aeth_ext.errors.shutdown import ShutdownKind, run_shutdown
from aeth_ext.errors.traceback_image import render_exception_image
from aeth_ext.settings import BaseSettings
from aeth_ext.static_eval import get_entrypoint_root, parse_and_grab_constants

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Coroutine, Generator
  from typing import Any


logger = getLogger(__name__)

__all__ = [
  "alert",
  "alert_exception",
  "handle_fatal_exc_async",
  "handle_fatal_exc_sync",
  "report_exc",
  "trigger_shutdown",
]


SETTINGS = BaseSettings.get_settings()

# Pushover priority for fatal alerts: "high" -- bypasses quiet hours but
# doesn't require acknowledgment. Non-fatal (alert_exception) alerts use the
# send_alert_push default (0, normal) since they don't affect control flow.
_FATAL_PUSH_PRIORITY = 1


@cache
def _resolve_program_name() -> str:
  """Identify which program raised the exception, so alerts remain attributable
  when several programs built on aeth_ext share the same alert inbox/Pushover
  key.

  Resolved from the host application's own `PROJECT_NAME` constant (see
  `parse_and_grab_constants`), not aeth_ext's. If no such constant is defined
  anywhere in its package ancestry, falls back to a best-effort guess -- the
  entrypoint script/package's own directory name -- rather than reporting
  nothing useful at all.

  Deliberately lazy (only run on first alert, then cached) rather than
  resolved at module import time: `err_handling` is imported transitively by
  every `import aeth_ext`, and `aeth_ext.logging.bases` already performs this
  same ancestry search independently for its own purposes (a circular import
  between the `errors` and `logging` packages rules out sharing that result
  directly) -- so deferring this one until an alert is actually sent avoids
  making every aeth_ext consumer pay for a second file-parsing search (and a
  second evaluation of the constant's value expression) that most of them will
  never need.
  """
  consts = parse_and_grab_constants(expected_constants={"PROJECT_NAME": "project_name"}, caller_file=__file__)
  return consts.get("project_name") or basename(get_entrypoint_root()) or "unknown"


# Distinguishes alerts from different machines running the same program name
# (e.g. multiple deployments/replicas sharing one alerts inbox/Pushover key).
# Cheap stdlib call with no parsing/eval involved, so no reason to defer it.
_HOSTNAME: str = gethostname()


def alert(reason: str, details: str, *, priority: int = 0, with_traceback: bool = True) -> None:
  """Send an alert through every configured out-of-band channel.

  Each underlying sender already catches and logs its own failures, so one
  channel being down (e.g. the alerts inbox locked out by a security policy)
  can't prevent the others from firing.

  *reason* and *details* are never passed to the underlying senders raw --
  they're folded into a standardized subject/title and body/message here
  (rather than by each caller) so every alert, e-mail and Pushover alike,
  reports the same information in the same shape:

  - *reason*: a short, human-readable description of why this alert is
    firing (e.g. ``"Fatal exception in worker_main"``). Becomes the tail of
    the subject line/title, after a fixed ``"Alert: [<program>] "`` prefix --
    the constant leading ``"Alert:"`` token is what lets a mail rule filter
    these out of the alerts inbox reliably, regardless of *reason*'s wording.
  - *details*: the substantive body beyond that standard header -- typically
    the exception's message and traceback (see :func:`_format_details`).
  - :func:`_resolve_program_name` -- which program raised the exception.
  - :data:`_HOSTNAME` -- which machine it was running on.
  - The current time (in :attr:`SETTINGS.tz`, matching the timezone used
    elsewhere for log timestamps) -- since alert delivery can lag behind the
    moment the exception actually occurred.

  *with_traceback* controls whether a traceback image is attached; leave it
  ``True`` (the default) only when called while the exception being reported
  is still the currently-handled one (i.e. from inside its own ``except``
  block), since the image is rendered from ``sys.exc_info()``. Callers
  reporting a plain condition with no live exception (e.g. a rejected
  handshake) must pass ``False``.
  """
  program_name = _resolve_program_name()
  timestamp = datetime.now(tz=SETTINGS.tz).isoformat(timespec="seconds")
  subject = f"Alert: [{program_name}] {reason}"
  body = f"Program: {program_name}\nHost: {_HOSTNAME}\nTime: {timestamp}\n\n{details}"
  image = render_exception_image() if with_traceback else None
  send_alert_email(subject, body, image=image)
  send_alert_push(subject, body, priority=priority, image=image)


def _extract_rich_traceback() -> str:
  """Render the currently-handled exception's traceback as plain text.

  Must be called from inside the ``except`` block for the exception being
  rendered, since Rich pulls it from ``sys.exc_info()``.
  """
  strio = StringIO()
  console = Console(force_terminal=False, force_interactive=False, color_system=None, markup=False, file=strio, no_color=True)
  with console.capture() as capture:
    console.print_exception(show_locals=True)
  return capture.get()


def _handle_fatal(label: str, exc: BaseException) -> None:
  """Log, alert, and drive a fatal shutdown for *exc*, the exception currently being handled as *label*'s failure.

  Shared by :func:`report_exc`, :func:`handle_fatal_exc_sync`, and
  :func:`handle_fatal_exc_async` -- the three of them differ only in how they
  catch the exception (context manager vs. sync/async decorator) and in what
  they do with it afterward (reraise vs. swallow and return ``None``), not in
  how the failure itself gets reported.

  Must be called from inside the ``except`` block for *exc*, since the
  traceback is rendered from ``sys.exc_info()``.
  """
  logger.critical("Fatal exception in %s", label, exc_info=exc)
  traceback_text = _extract_rich_traceback()
  alert(f"Fatal exception in {label}", f"{exc}:\n\n{traceback_text}", priority=_FATAL_PUSH_PRIORITY)
  run_shutdown(ShutdownKind.FATAL)


def trigger_shutdown(reason: str, details: str, *, kind: ShutdownKind = ShutdownKind.FATAL) -> None:
  """Alert then drive a shutdown for a condition with no live exception to report.

  Unlike :func:`report_exc`/:func:`handle_fatal_exc_sync`/:func:`handle_fatal_exc_async`, this
  needs no synthetic ``raise`` to reach :func:`~aeth_ext.errors.shutdown.run_shutdown` -- for a
  caller that has already determined the process must go down from a plain condition (e.g. a
  rejected remote logging config, D-E6), fabricating an exception just to reuse those helpers'
  machinery would be needless indirection. *reason*/*details* are passed straight through to
  :func:`alert` with ``with_traceback=False``, since there is no traceback to render.

  No-op when running under the default CPython interpreter (``__debug__ == True``), matching
  every other fatal-condition helper in this module.
  """
  if __debug__:
    return
  logger.critical(reason)
  alert(reason, details, priority=_FATAL_PUSH_PRIORITY, with_traceback=False)
  run_shutdown(kind)


def alert_exception(label: str, exc: BaseException) -> None:
  """Send an alert e-mail for *exc* without marking the process as fatally broken.

  Unlike :func:`report_exc`/:func:`handle_fatal_exc_sync`/:func:`handle_fatal_exc_async`,
  this never requests a shutdown and never affects control flow — it
  doesn't catch, re-raise, or swallow anything. It's a single-purpose
  notification primitive: call it explicitly from inside an ``except`` block
  for a failure that should be e-mailed but must not bring the rest of the
  process down, e.g. a per-connection handler that already isolates its own
  failures and must keep running for other connections.

  Must be called while *exc* is the currently-handled exception (i.e. from
  inside its own ``except`` block), since the traceback is rendered from
  ``sys.exc_info()``.

  Like :func:`report_exc` and the decorators, this is a no-op when running
  under the default CPython interpreter (``__debug__ == True``) so that
  exceptions surface naturally during development.
  """
  if __debug__:
    return
  traceback_text = _extract_rich_traceback()
  alert(f"Non-fatal exception in {label}", f"{exc}:\n\n{traceback_text}")


@contextmanager
def report_exc(label: str, *, reraise: bool = False) -> Generator[None]:
  """Context-manager counterpart of the :func:`handle_fatal_exc_sync` decorator.

  Catches any non-cancellation exception raised inside the ``with`` block,
  logs it as critical, sends an alert e-mail, and drives a fatal shutdown.
  The exception is then re-raised (default) or swallowed, controlled by
  *reraise*.

  Pass ``reraise=False`` at error boundaries that must not propagate the
  exception to the caller — for example inside a logging
  :meth:`~logging.Handler.emit` implementation, where a propagating exception
  would crash the thread that emitted the record.

  Prefer :func:`trigger_shutdown` instead when there is no exception to raise
  in the first place -- e.g. a rejected remote logging config (D-E6), which
  needs no synthetic exception at all.

  Like the decorators, this is a no-op when running under the default CPython
  interpreter (``__debug__ == True``) so that exceptions surface naturally
  during development.
  """
  if __debug__:
    yield
    return
  try:
    yield
  except CancelledError:
    raise
  except BaseException as e:
    _handle_fatal(label, e)
    if reraise:
      raise


@overload
def handle_fatal_exc_sync[**Params_T, Return_T](
  func: None = ..., *, extract_details_callable: Callable[[BaseException], Any]
) -> Callable[[Callable[Params_T, Return_T]], Callable[Params_T, Return_T | None]]: ...


@overload
def handle_fatal_exc_sync[**Params_T, Return_T](
  func: Callable[Params_T, Return_T], *, extract_details_callable: None = ...
) -> Callable[Params_T, Return_T | None]: ...


def handle_fatal_exc_sync[**Params_T, Return_T](
  func: Callable[Params_T, Return_T] | None = None,
  *,
  extract_details_callable: Callable[[BaseException], Any] | None = None,
) -> Callable[Params_T, Return_T | None] | Callable[[Callable[Params_T, Return_T]], Callable[Params_T, Return_T | None]]:
  def decorator(
    func: Callable[Params_T, Return_T],
  ) -> Callable[Params_T, Return_T | None]:

    @wraps(func)
    def wrapper(*args: Params_T.args, **kwargs: Params_T.kwargs) -> Return_T | None:
      try:
        return func(*args, **kwargs)
      except CancelledError:
        raise  # raise whatever to make the type checker happy about return values
      except BaseException as e:  # noqa: BLE001 -- fully handled by _handle_fatal (logs, alerts, drives shutdown)
        if extract_details_callable is not None:
          try:
            extract_details_callable(e)
          except Exception as extract_exc:
            logger.exception("Error in extract_details_callable for exception", exc_info=extract_exc)
        _handle_fatal(func.__qualname__, e)
        return None

    return func if __debug__ and __name__ != "__main__" else wrapper

  if func is not None:
    return decorator(func)

  return decorator


@overload
def handle_fatal_exc_async[**Params_T, Return_T](
  func: None = ..., *, extract_details_callable: Callable[[BaseException], Any]
) -> Callable[[Callable[Params_T, Coroutine[None, None, Return_T]]], Callable[Params_T, Coroutine[None, None, Return_T | None]]]: ...


@overload
def handle_fatal_exc_async[**Params_T, Return_T](
  func: Callable[Params_T, Coroutine[None, None, Return_T]], *, extract_details_callable: None = ...
) -> Callable[Params_T, Coroutine[None, None, Return_T | None]]: ...


def handle_fatal_exc_async[**Params_T, Return_T](
  func: Callable[Params_T, Coroutine[None, None, Return_T]] | None = None,
  *,
  extract_details_callable: Callable[[BaseException], Any] | None = None,
) -> (
  Callable[Params_T, Coroutine[None, None, Return_T | None]]
  | Callable[[Callable[Params_T, Coroutine[None, None, Return_T]]], Callable[Params_T, Coroutine[None, None, Return_T | None]]]
):
  def decorator(
    func: Callable[Params_T, Coroutine[None, None, Return_T]],
  ) -> Callable[Params_T, Coroutine[None, None, Return_T | None]]:
    @wraps(func)
    async def wrapper(*args: Params_T.args, **kwargs: Params_T.kwargs) -> Return_T | None:
      try:
        return await func(*args, **kwargs)
      except CancelledError:
        raise  # raise whatever to make the type checker happy about return values
      except GeneratorExit:
        return None  # if a GeneratorExit is caught, that means a coroutine is being cancelled for a graceful shutdown.
      except BaseException as e:  # noqa: BLE001 -- fully handled by _handle_fatal (logs, alerts, drives shutdown)
        if extract_details_callable is not None:
          try:
            extract_details_callable(e)
          except Exception as extract_exc:
            logger.exception("Error in extract_details_callable for exception", exc_info=extract_exc)
        _handle_fatal(func.__qualname__, e)
        return None

    return func if __debug__ and __name__ != "__main__" else wrapper

  if func is not None:
    return decorator(func)

  return decorator


def testing_details_extractor(exc: BaseException) -> None:
  pass


if __name__ == "__main__":
  # live test of alert()
  try:
    raise RuntimeError("test exception")
  except Exception:  # noqa: BLE001
    alert("Test alert", "See attached traceback for details", priority=_FATAL_PUSH_PRIORITY)
