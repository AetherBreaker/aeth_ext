# Standard library imports
from typing import TYPE_CHECKING, Literal, overload

# First party imports
from aeth_ext.monkey_patcher import MonkeyPatcher
from aeth_ext.static_eval import get_caller_file

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
  caller_file: str | None = None,
) -> None: ...


@overload
def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  return_wrapped: Literal[True],
  caller_file: str | None = None,
) -> Callable[[], None]: ...


def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  return_wrapped: bool = False,
  caller_file: str | None = None,
) -> None | Callable[[], None]:
  # Resolved once, here, at the true call site -- not inside wrapped_initialize()
  # or any of the functions it calls -- so it reflects the real entrypoint script
  # rather than this closure's own frame. Every function below receives it
  # explicitly and never re-detects it on its own.
  if caller_file is None:
    caller_file = get_caller_file(1)

  def wrapped_initialize() -> None:
    if run_monkey_patches:
      MonkeyPatcher.apply_monkey_patches(caller_file=caller_file)

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
        init_logging_socket(caller_file=caller_file)
      case "to_queue":
        init_logging_to_queue(queues[0], caller_file=caller_file)
      case True:
        init_logging(*queues, asyncio=asyncio, caller_file=caller_file)
      case _:
        pass

  if return_wrapped:
    return wrapped_initialize

  wrapped_initialize()
