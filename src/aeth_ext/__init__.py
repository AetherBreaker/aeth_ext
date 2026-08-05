# Standard library imports
from typing import TYPE_CHECKING, Literal, overload

# First party imports
from aeth_ext.errors import SHUTDOWN_EVENT
from aeth_ext.monkey_patcher import MonkeyPatcher
from aeth_ext.static_eval import get_caller_file

# Local folder imports
from .logging.init import init_logging, init_logging_socket, init_logging_to_queue

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from types import FrameType

  # Local folder imports
  from .logging.setup import QueueCatchall

__all__ = ["initialize"]


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
  """Set :data:`~aeth_ext.errors.SHUTDOWN_EVENT` in response to a caught OS signal (D-G2).

  Only sets the event and returns control to whatever was running - it does
  not itself block or perform any shutdown work. Consumers subscribed to the
  event own their own drain/flush sequences (D-G4; see TODO.md for the
  still-unwired consumer side).
  """
  SHUTDOWN_EVENT.set()


def _register_shutdown_signal_handlers() -> None:
  """Register OS signal handlers that set :data:`~aeth_ext.errors.SHUTDOWN_EVENT` (D-G2/D-G3).

  Platform dispatch mirrors the ``platform in (...)`` branch used above for
  the event loop policy choice. POSIX registers ``SIGINT``/``SIGTERM`` via
  :func:`signal.signal`. Windows registers ``SIGINT`` and, where available,
  ``SIGBREAK`` - Windows has no OS-level ``SIGTERM`` delivery mechanism, so
  nothing is lost by not registering it there. This stays within the
  standard-library :mod:`signal` module; no ``pywin32``/console-control-handler
  dependency is introduced.
  """
  # Standard library imports
  import signal
  from sys import platform

  signal.signal(signal.SIGINT, _handle_shutdown_signal)
  if platform == "win32":
    if hasattr(signal, "SIGBREAK"):
      signal.signal(signal.SIGBREAK, _handle_shutdown_signal)
  else:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)


def _install_event_loop_policy() -> None:
  """Install winloop/uvloop as the process's asyncio event loop policy.

  Installing the *policy* (rather than pre-building and ``set_event_loop()``-ing
  a loop instance) means every subsequent loop-creation path picks up
  winloop/uvloop automatically -- including ``asyncio.run()``/``Runner``, which
  always build their own loop via ``events.new_event_loop()`` and ignore
  whatever ``set_event_loop()`` installed.

  Deprecated since 3.14, removed in 3.16 -- interim choice until `initialize()`
  is reworked to own startup and hand back a ``loop_factory`` instead (see
  TODO.md #2).
  """
  # Standard library imports
  from sys import platform
  from warnings import catch_warnings, filterwarnings

  # Both the `EventLoopPolicy` import (winloop's __getattr__ touches the deprecated
  # asyncio.AbstractEventLoopPolicy lazily) and the set_event_loop_policy() call below
  # raise DeprecationWarning on 3.14+. Filtered by message rather than
  # simplefilter("ignore", DeprecationWarning) so only these specific warnings are
  # caught -- any unrelated DeprecationWarning raised during this block (e.g. from
  # deeper in winloop's/uvloop's own import chain) still surfaces normally.
  with catch_warnings():
    filterwarnings("ignore", category=DeprecationWarning, message=r"(?i).*event.?loop.?policy.*")

    if platform in ("win32", "cygwin", "cli"):
      # Third party imports
      from winloop import EventLoopPolicy
    else:
      # if we're on apple or linux do this instead
      # Third party imports
      from uvloop import EventLoopPolicy  # type: ignore
    # Standard library imports
    from asyncio import set_event_loop_policy  # pyright: ignore[reportDeprecated]

    set_event_loop_policy(EventLoopPolicy())  # pyright: ignore[reportDeprecated]


@overload
def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  install_signal_handlers: bool = True,
  return_wrapped: Literal[False] = False,
  caller_file: str | None = None,
) -> None: ...


@overload
def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  install_signal_handlers: bool = True,
  return_wrapped: Literal[True],
  caller_file: str | None = None,
) -> Callable[[], None]: ...


def initialize(
  *queues: QueueCatchall,
  logging: bool | Literal["socket", "to_queue"] = True,
  asyncio: bool = False,
  run_monkey_patches: bool = True,
  install_signal_handlers: bool = True,
  return_wrapped: bool = False,
  caller_file: str | None = None,
) -> Callable[[], None] | None:
  # Resolved once, here, at the true call site -- not inside wrapped_initialize()
  # or any of the functions it calls -- so it reflects the real entrypoint script
  # rather than this closure's own frame. Every function below receives it
  # explicitly and never re-detects it on its own.
  if caller_file is None:
    caller_file = get_caller_file(1)

  def wrapped_initialize() -> None:
    if run_monkey_patches:
      MonkeyPatcher.apply_monkey_patches(caller_file=caller_file)

    if install_signal_handlers:
      _register_shutdown_signal_handlers()

    if asyncio:
      _install_event_loop_policy()

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
