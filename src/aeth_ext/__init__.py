# Standard library imports
from typing import TYPE_CHECKING, Literal, overload

# First party imports
from aeth_ext.monkey_patcher import MonkeyPatcher

# Local folder imports
from .logging.init import init_logging, init_logging_worker

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # Local folder imports
  from .logging.config import QueueCatchall

__all__ = ["initialize"]


@overload
def initialize(
  *queues: QueueCatchall,
  asyncio: bool = False,
  worker: bool = False,
  run_monkey_patches: bool = True,
  return_wrapped: Literal[False] = False,
) -> None: ...
@overload
def initialize(
  *queues: QueueCatchall,
  asyncio: bool = False,
  worker: bool = False,
  run_monkey_patches: bool = True,
  return_wrapped: Literal[True],
) -> Callable[[], None]: ...
def initialize(
  *queues: QueueCatchall,
  asyncio: bool = False,
  worker: bool = False,
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

    if worker:
      init_logging_worker(queues[0])
    else:
      init_logging(*queues)

  if return_wrapped:
    return wrapped_initialize

  wrapped_initialize()
