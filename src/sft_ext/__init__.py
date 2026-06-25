# Standard library imports
from typing import TYPE_CHECKING, Any

# Local folder imports
from .logging.init import init_logging, init_logging_worker

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # Local folder imports
  from .logging.config import QueueCatchall

__all__ = ["initialize", "initialize_async", "initialize_async_worker", "initialize_worker"]


def initialize(*queues: QueueCatchall, monkey_patch_hook: Callable[..., Any] | None = None) -> None:
  """
  Initializes the logging system for the application.

  This function sets up the logging configuration, including log levels, handlers, and formatters.
  It should be called at the start of the application to ensure that logging is properly configured.

  Args:
      *queues (QueueCatchall): Optional queues for logging in multi-process or multi-threaded
      environments. If provided, logging will be configured to use these queues for log message handling.
      monkey_patch_hook (Callable[..., Any] | None): An optional hook for monkey patching. If provided,
      this hook will be called first during initialization.
  """
  if monkey_patch_hook is not None:
    monkey_patch_hook()
  init_logging(*queues)


def initialize_worker(queue: QueueCatchall, monkey_patch_hook: Callable[..., Any] | None = None) -> None:
  """
  Initializes the logging system for worker processes.

  This function sets up the logging configuration for worker processes, including log levels, handlers, and formatters.
  It should be called at the start of each worker process to ensure that logging is properly configured.

  Args:
      queue (QueueCatchall): A queue for logging in multi-process or multi-threaded
      environments. This queue will be used to handle log messages from the worker process.
      monkey_patch_hook (Callable[..., Any] | None): An optional hook for monkey patching. If provided,
      this hook will be called first during initialization.
  """
  if monkey_patch_hook is not None:
    monkey_patch_hook()
  init_logging_worker(queue)


def _async_init() -> None:
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


def initialize_async(*queues: QueueCatchall, monkey_patch_hook: Callable[..., Any] | None = None) -> None:
  """
  Initializes the logging system for asynchronous applications.

  This function sets up the logging configuration for asynchronous applications, including log levels, handlers, and formatters.
  It should be called at the start of the application to ensure that logging is properly configured.

  Args:
      *queues (QueueCatchall): Optional queues for logging in multi-process or multi-threaded
      environments. If provided, logging will be configured to use these queues for log message handling.
      monkey_patch_hook (Callable[..., Any] | None): An optional hook for monkey patching. If provided,
      this hook will be called first during initialization.
  """
  _async_init()
  initialize(*queues, monkey_patch_hook=monkey_patch_hook)


def initialize_async_worker(queue: QueueCatchall, monkey_patch_hook: Callable[..., Any] | None = None) -> None:
  """
  Initializes the logging system for asynchronous worker processes.

  This function sets up the logging configuration for asynchronous worker processes, including log levels, handlers, and formatters.
  It should be called at the start of each worker process to ensure that logging is properly configured.

  Args:
      queue (QueueCatchall): A queue for logging in multi-process or multi-threaded
      environments. This queue will be used to handle log messages from the worker process.
      monkey_patch_hook (Callable[..., Any] | None): An optional hook for monkey patching. If provided,
      this hook will be called first during initialization.
  """
  _async_init()
  initialize_worker(queue, monkey_patch_hook=monkey_patch_hook)
