# Standard library imports
from asyncio import CancelledError
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
  from collections.abc import Callable, Coroutine
  from typing import Any


logger = getLogger(__name__)

__all__ = ["FATAL_EVENT", "handle_fatal_exc_async", "handle_fatal_exc_sync"]


FATAL_EVENT = Event()


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
        pass
        raise  # raise whatever to make the type checker happy about return values
      except BaseException as e:
        if isinstance(e, CancelledError):
          raise
        logger.critical("Fatal exception in %s", func.__qualname__, exc_info=e)

        strio = StringIO()

        tmp = Console(force_terminal=False, force_interactive=False, color_system=None, markup=False, file=strio, no_color=True)

        with tmp.capture() as capture:
          tmp.print_exception(show_locals=True)
        content = capture.get()

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
        pass
        raise  # raise whatever to make the type checker happy about return values
      except GeneratorExit:
        pass
        return None  # if a GeneratorExit is caught, that means a coroutine is being cancelled for a graceful shutdown.
      except BaseException as e:
        if isinstance(e, CancelledError):
          raise
        if extract_details_callable is not None:
          try:
            extract_details_callable(e)
          except Exception as extract_exc:
            logger.exception("Error in extract_details_callable for exception", exc_info=extract_exc)
        logger.critical("Fatal exception in %s", func.__qualname__, exc_info=e)

        strio = StringIO()

        tmp = Console(force_terminal=False, force_interactive=False, color_system=None, markup=False, file=strio, no_color=True)

        with tmp.capture() as capture:
          tmp.print_exception(show_locals=True)
        content = capture.get()

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
