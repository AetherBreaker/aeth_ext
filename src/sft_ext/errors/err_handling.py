import sys
from asyncio import CancelledError
from collections.abc import Callable, Coroutine
from functools import wraps
from io import StringIO
from logging import getLogger

from aiologic import Event
from rich.console import Console
from sft_ext.errors.send_alert_email import send_alert_email

main_module = sys.modules["__main__"]
RICH_CONSOLE = getattr(main_module, "RICH_CONSOLE", None)

logger = getLogger(__name__)


FATAL_EVENT = Event()


def handle_fatal_exc_sync[**TP, TR](func: Callable[TP, TR]) -> Callable[TP, TR | None]:
  @wraps(func)
  def wrapper(*args: TP.args, **kwargs: TP.kwargs) -> TR | None:
    try:
      return func(*args, **kwargs)
    except CancelledError:
      pass
      raise  # raise whatever to make the type checker happy about return values
    except BaseException as e:
      if isinstance(e, CancelledError):
        raise
      logger.critical(f"Fatal exception in {func.__qualname__}: {e}", exc_info=True)

      strio = StringIO()

      tmp = Console(force_terminal=False, force_interactive=False, color_system=None, markup=False, file=strio, no_color=True)

      with tmp.capture() as capture:
        tmp.print_exception(show_locals=True)
      content = capture.get()

      send_alert_email(f"Fatal exception in {func.__qualname__}", f"{e}:\n\n{content}")
      FATAL_EVENT.set()
      return None

  return func if __debug__ and __name__ != "__main__" else wrapper


def handle_fatal_exc_async[**TP, TR](func: Callable[TP, Coroutine[None, None, TR]]) -> Callable[TP, Coroutine[None, None, TR | None]]:
  @wraps(func)
  async def wrapper(*args: TP.args, **kwargs: TP.kwargs) -> TR | None:
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
      logger.critical(f"Fatal exception in {func.__qualname__}: {e}", exc_info=True)

      strio = StringIO()

      tmp = Console(force_terminal=False, force_interactive=False, color_system=None, markup=False, file=strio, no_color=True)

      with tmp.capture() as capture:
        tmp.print_exception(show_locals=True)
      content = capture.get()

      send_alert_email(f"Fatal exception in {func.__qualname__}", f"{e}:\n\n{content}")
      FATAL_EVENT.set()
      return None

  return func if __debug__ and __name__ != "__main__" else wrapper


if __name__ == "__main__":

  @handle_fatal_exc_sync
  def test_func():
    raise ValueError("This is a test exception.")

  test_func()
