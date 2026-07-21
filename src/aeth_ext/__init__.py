# Standard library imports
from typing import TYPE_CHECKING, Literal, overload

# First party imports
from aeth_ext.monkey_patcher import MonkeyPatcher

# Local folder imports
from .logging.init import init_logging, init_logging_socket, init_logging_to_queue

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # Local folder imports
  from .logging.setup import QueueCatchall

__all__ = ["initialize"]


@overload
def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  return_wrapped: Literal[False] = False,
) -> None: ...


@overload
def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  return_wrapped: Literal[True],
) -> Callable[[], None]: ...


def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  return_wrapped: bool = False,
) -> None | Callable[[], None]:

  def wrapped_initialize() -> None:
    if run_monkey_patches:
      MonkeyPatcher.apply_monkey_patches()

    if asyncio:
      # Standard library imports
      from sys import platform

      if platform in ("win32", "cygwin", "cli"):
        # Third party imports
        from winloop import new_event_loop
      else:
        # if we're on apple or linux do this instead
        # Third party imports
        from uvloop import new_event_loop  # type: ignore
      # Standard library imports
      from asyncio import set_event_loop

      set_event_loop(new_event_loop())

    match logging:
      case "socket":
        init_logging_socket()

      case "to_queue":
        init_logging_to_queue(queues[0])
      case True:
        init_logging(*queues, asyncio=asyncio)
      case _:
        pass

  if return_wrapped:
    return wrapped_initialize

  wrapped_initialize()
