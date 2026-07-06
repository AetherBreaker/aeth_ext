# Standard library imports
import sys
from annotationlib import Format
from inspect import Parameter, signature
from typing import TYPE_CHECKING

# Third party imports
from rich import get_console
from rich.console import Console

# First party imports
from aeth_ext.logging.config import BaseLoggingConfig
from aeth_ext.static_eval import parse_and_grab_constants

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from typing import Any

  # First party imports
  from aeth_ext.logging.config import QueueCatchall


__all__ = ["init_logging", "init_logging_worker"]

__initialized = False

__used_locals = {"sys": sys, "platform": sys.platform, "Console": Console}


def __init_logging_base(
  func_target: Callable[..., Any],
  queues: QueueCatchall | tuple[QueueCatchall, ...] | None = None,
  asyncio: bool | None = None,
) -> None:
  """
  Handles the initialization of logging for the entire project.
  It will attempt to find any uppercase constants defined in __main__ that match the parameter names of the configure_logging function,
  and use those values to configure logging.\n
  If the expected constants are not found in __main__, it will attempt to fall back to the app's dedicated entrypoint script __main__.py
  to find those constants.\n
  This allows for flexible configuration of logging behavior without requiring changes to this module or the logging_config module.
  """
  global __initialized
  if __initialized:
    return

  found_kwargs: dict[str, Any] = {}
  if queues is not None:
    found_kwargs["logging_queues"] = queues
  if asyncio is not None:
    found_kwargs["asyncio"] = asyncio
  uppered_kwargs = {}
  expected_kwargs = {}

  sig = signature(func_target, annotation_format=Format.FORWARDREF)

  for param in sig.parameters.values():
    if param.name in found_kwargs:
      continue
    expected_kwargs[param.name] = param
    found_kwargs[param.name] = Parameter.empty if param.default is Parameter.empty else param.default
    uppered_kwargs[param.name.upper()] = param.name

  parsed_kwargs = parse_and_grab_constants(expected_constants=uppered_kwargs, eval_locals=__used_locals)

  for kwarg_name, kwarg_value in parsed_kwargs.items():
    if kwarg_name not in found_kwargs or found_kwargs[kwarg_name] is Parameter.empty or found_kwargs[kwarg_name] is None:
      found_kwargs[kwarg_name] = kwarg_value

  if "rich_console" in found_kwargs:
    rich_shared_console = get_console()

    if isinstance(found_console := found_kwargs.get("rich_console"), Console):
      rich_shared_console.__dict__ = found_console.__dict__

    elif found_console is not None and found_console is not Parameter.empty:
      raise TypeError(f"Expected 'rich_console' to be of type Console, but got {type(found_console)}")

    found_kwargs["rich_console"] = rich_shared_console

  if any(
    value is Parameter.empty or (value is None and name in expected_kwargs and expected_kwargs[name].default is Parameter.empty)
    for name, value in found_kwargs.items()
  ):
    missing_args = [
      name
      for name, value in found_kwargs.items()
      if value is Parameter.empty or (value is None and name in expected_kwargs and expected_kwargs[name].default is Parameter.empty)
    ]
    raise ValueError(f"Missing required logging configuration arguments: {', '.join(missing_args)}")

  func_target(**found_kwargs)
  __initialized = True


def init_logging(*queues: QueueCatchall, asyncio: bool = False) -> None:
  """
  Initializes logging for the entire project. This should be called at the very beginning of the main entrypoint of the application.
  It will attempt to find any uppercase constants defined in __main__ that match the parameter names of the configure_logging function,
  and use those values to configure logging.\n
  If the expected constants are not found in __main__, it will attempt to fall back to the app's dedicated entrypoint script __main__.py
  to find those constants.\n
  This allows for flexible configuration of logging behavior without requiring changes to this module or the logging_config module.
  """
  config_cls = BaseLoggingConfig.get_deepest_subclass()

  __init_logging_base(func_target=config_cls.configure_logging_main, queues=queues, asyncio=asyncio)


def init_logging_worker(queue: QueueCatchall) -> None:
  """
  Handles the initialization of logging for worker processes.
  It will attempt to find any uppercase constants defined in __main__ that match the parameter names of the configure_logging function,
  and use those values to configure logging.\n
  If the expected constants are not found in __main__, it will attempt to fall back to the app's dedicated entrypoint script __main__.py
  to find those constants.\n
  This allows for flexible configuration of logging behavior without requiring changes to this module or the logging_config module.
  """
  config_cls = BaseLoggingConfig.get_deepest_subclass()

  __init_logging_base(func_target=config_cls.configure_logging_worker, queues=queue)


def init_logging_socket() -> None:
  """
  Initializes logging for the entire project. This should be called at the very beginning of the main entrypoint of the application.
  It will attempt to find any uppercase constants defined in __main__ that match the parameter names of the configure_logging function,
  and use those values to configure logging.\n
  If the expected constants are not found in __main__, it will attempt to fall back to the app's dedicated entrypoint script __main__.py
  to find those constants.\n
  This allows for flexible configuration of logging behavior without requiring changes to this module or the logging_config module.
  """
  config_cls = BaseLoggingConfig.get_deepest_subclass()

  __init_logging_base(func_target=config_cls.configure_shared_socket_logging_client)
