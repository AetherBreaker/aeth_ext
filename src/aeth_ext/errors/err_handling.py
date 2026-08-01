# Standard library imports
from asyncio import CancelledError
from contextlib import contextmanager
from functools import wraps
from io import StringIO
from logging import getLogger
from typing import TYPE_CHECKING, overload

# Third party imports
from aiologic import Event
from rich.console import Console

# First party imports
from aeth_ext.errors.send_alert_email import send_alert_email

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Coroutine, Generator
  from typing import Any


logger = getLogger(__name__)

__all__ = ["FATAL_EVENT", "alert_exception", "handle_fatal_exc_async", "handle_fatal_exc_sync", "report_exc"]


FATAL_EVENT = Event()


def _format_exception_traceback() -> str:
  """Render the currently-handled exception's traceback as plain text.

  Must be called from inside the ``except`` block for the exception being
  rendered, since Rich pulls it from ``sys.exc_info()``.
  """
  strio = StringIO()
  console = Console(force_terminal=False, force_interactive=False, color_system=None, markup=False, file=strio, no_color=True)
  with console.capture() as capture:
    console.print_exception(show_locals=True)
  return capture.get()


def alert_exception(label: str, exc: BaseException) -> None:
  """Send an alert e-mail for *exc* without marking the process as fatally broken.

  Unlike :func:`report_exc`/:func:`handle_fatal_exc_sync`/:func:`handle_fatal_exc_async`,
  this never sets :data:`FATAL_EVENT` and never affects control flow — it
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
  content = _format_exception_traceback()
  send_alert_email(f"Exception in {label}", f"{exc}:\n\n{content}")


@contextmanager
def report_exc(label: str, *, reraise: bool = False) -> Generator[None]:
  """Context-manager counterpart of the :func:`handle_fatal_exc_sync` decorator.

  Catches any non-cancellation exception raised inside the ``with`` block,
  logs it as critical, sends an alert e-mail, and sets :data:`FATAL_EVENT`.
  The exception is then re-raised (default) or swallowed, controlled by
  *reraise*.

  Pass ``reraise=False`` at error boundaries that must not propagate the
  exception to the caller — for example inside a logging
  :meth:`~logging.Handler.emit` implementation, where a propagating exception
  would crash the thread that emitted the record.

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
    logger.critical("Fatal exception in %s", label, exc_info=e)
    content = _format_exception_traceback()
    send_alert_email(f"Fatal exception in {label}", f"{e}:\n\n{content}")
    FATAL_EVENT.set()
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
      except BaseException as e:
        if extract_details_callable is not None:
          try:
            extract_details_callable(e)
          except Exception as extract_exc:
            logger.exception("Error in extract_details_callable for exception", exc_info=extract_exc)
        logger.critical("Fatal exception in %s", func.__qualname__, exc_info=e)
        content = _format_exception_traceback()
        send_alert_email(f"Fatal exception in {func.__qualname__}", f"{e}:\n\n{content}")
        FATAL_EVENT.set()
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
      except BaseException as e:
        if extract_details_callable is not None:
          try:
            extract_details_callable(e)
          except Exception as extract_exc:
            logger.exception("Error in extract_details_callable for exception", exc_info=extract_exc)
        logger.critical("Fatal exception in %s", func.__qualname__, exc_info=e)
        content = _format_exception_traceback()
        send_alert_email(f"Fatal exception in {func.__qualname__}", f"{e}:\n\n{content}")
        FATAL_EVENT.set()
        return None

    return func if __debug__ and __name__ != "__main__" else wrapper

  if func is not None:
    return decorator(func)

  return decorator


def testing_details_extractor(exc: BaseException) -> None:
  pass


if __name__ == "__main__":

  @handle_fatal_exc_sync
  def test_func():
    # sourcery skip: no-conditionals-in-tests
    if __debug__:
      raise ValueError("This is a test exception.")

  test_func()

  @handle_fatal_exc_sync(extract_details_callable=testing_details_extractor)
  def test_func_with_details():
    # sourcery skip: no-conditionals-in-tests
    if __debug__:
      raise ValueError("This is a test exception with details.")

  test_func_with_details()

  # Standard library imports
  import asyncio

  @handle_fatal_exc_async
  async def test_async_func():
    # sourcery skip: no-conditionals-in-tests
    if __debug__:
      raise ValueError("This is a test async exception.")

  asyncio.run(test_async_func())

  @handle_fatal_exc_async(extract_details_callable=testing_details_extractor)
  async def test_async_func_with_details():
    # sourcery skip: no-conditionals-in-tests
    if __debug__:
      raise ValueError("This is a test async exception with details.")

  asyncio.run(test_async_func_with_details())
