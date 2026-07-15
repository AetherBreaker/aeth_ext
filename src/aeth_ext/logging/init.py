# Standard library imports
import sys
from annotationlib import Format
from inspect import Parameter, signature
from typing import TYPE_CHECKING

# Third party imports
from rich import get_console
from rich.console import Console

# First party imports
from aeth_ext.logging.setup import BaseLoggingConfig
from aeth_ext.static_eval import parse_and_grab_constants

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from typing import Any

  # First party imports
  from aeth_ext.logging.setup import QueueCatchall


__all__ = ["init_logging", "init_logging_worker"]

__initialized = False

__used_locals = {"sys": sys, "platform": sys.platform, "Console": Console}


def __resolve_rich_console[kwargs_T: dict[str, Any]](found_kwargs: kwargs_T) -> kwargs_T:
  """Resolves the rich_console kwarg to the shared Rich console instance."""
  rich_shared_console = get_console()
  found_console = found_kwargs.get("rich_console", None)

  if isinstance(found_console, Console):
    rich_shared_console.__dict__ = found_console.__dict__
  elif found_console is not None and found_console is not Parameter.empty:
    raise TypeError(f"Expected 'rich_console' to be of type Console, but got {type(found_console)}")

  found_kwargs["rich_console"] = rich_shared_console

  return found_kwargs


def __apply_defaults[kwargs_T: dict[str, Any]](found_kwargs: kwargs_T, expected_kwargs: dict[str, Parameter]) -> kwargs_T:
  """Applies default values for any parameters that were not found in the parsed constants."""
  missing_args = []

  for param_name, param in found_kwargs.items():
    # Anything that is still Parameter.empty at this point is missing and needs to be checked for a default value
    if param is Parameter.empty:
      # If the parameter has a default value, and it is not Parameter.empty, use that default value
      if (default := expected_kwargs[param_name].default) is not Parameter.empty:
        found_kwargs[param_name] = default
      else:
        # If the parameter does not have a default value, it is missing and should be added to the list of missing arguments
        missing_args.append(param_name)

  if missing_args:
    raise ValueError(f"Missing required arguments: {', '.join(missing_args)}")

  return found_kwargs


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

  # creating the base dict of parameters to find
  for param in sig.parameters.values():
    # skip the overrides already set above
    if param.name in found_kwargs:
      continue
    expected_kwargs[param.name] = param
    found_kwargs[param.name] = Parameter.empty
    uppered_kwargs[param.name.upper()] = param.name

  parsed_kwargs = parse_and_grab_constants(expected_constants=uppered_kwargs, eval_locals=__used_locals)

  # assign parsed values
  for kwarg_name, kwarg_value in parsed_kwargs.items():
    if kwarg_name not in found_kwargs or found_kwargs[kwarg_name] is Parameter.empty:
      found_kwargs[kwarg_name] = kwarg_value

  # rich_console will never have a default value, so we can resolve it before defaults
  if "rich_console" in found_kwargs:
    found_kwargs = __resolve_rich_console(found_kwargs)

  found_kwargs = __apply_defaults(found_kwargs, expected_kwargs)

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
