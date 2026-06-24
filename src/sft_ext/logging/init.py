# Standard library imports
import sys
from annotationlib import Format
from contextlib import suppress
from importlib import import_module
from inspect import Parameter, signature
from pathlib import Path
from typing import TYPE_CHECKING, cast

# Third party imports
from rich import get_console
from rich.console import Console

# First party imports
from sft_ext.const_parsing import parse_and_grab_constants

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from typing import Any

  # First party imports
  from sft_ext.logging import config
  from sft_ext.logging.config import QueueCatchall


__all__ = ["init_logging", "init_logging_worker"]

__initialized = False

__used_locals = {"sys": sys, "platform": sys.platform, "Console": Console}


def __init_logging_base(queues: QueueCatchall | tuple[QueueCatchall, ...], func_target: Callable[..., Any]) -> None:  # noqa: C901, PLR0912
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

  found_kwargs: dict[str, Any] = {
    "logging_queues": queues,
  }
  uppered_kwargs = {}

  for param in signature(func_target, annotation_format=Format.FORWARDREF).parameters.values():
    if param.name in found_kwargs:
      continue
    found_kwargs[param.name] = Parameter.empty if param.default is Parameter.empty else param.default
    uppered_kwargs[param.name.upper()] = param.name

  main_module_file_loc = None

  with suppress(KeyError, AttributeError):
    main_module_file_loc = Path(sys.modules["__main__"].__file__)  # type: ignore

    main_module_found_kwargs = parse_and_grab_constants(main_module_file_loc, uppered_kwargs, eval_locals=__used_locals)

    for kwarg_name, kwarg_value in main_module_found_kwargs.items():
      if kwarg_name not in found_kwargs or found_kwargs[kwarg_name] is Parameter.empty or found_kwargs[kwarg_name] is None:
        found_kwargs[kwarg_name] = kwarg_value

  maindotpy_file_loc = Path(sys.argv[0]).resolve()
  if maindotpy_file_loc.is_dir():
    maindotpy_file_loc = maindotpy_file_loc / "__main__.py"
  elif maindotpy_file_loc.name != "__main__.py":
    # search for /src/ within CWD, then recursive search through /src/ for a __main__.py file
    src_dir = Path.cwd() / "src"
    if src_dir.exists() and src_dir.is_dir():
      with suppress(FileNotFoundError, StopIteration):
        maindotpy_file_loc = next(src_dir.rglob("__main__.py"))

  if (
    maindotpy_file_loc.exists()
    and str(main_module_file_loc) != str(maindotpy_file_loc)
    and any(value is None for value in found_kwargs.values())
  ):
    maindotpy_found_kwargs = parse_and_grab_constants(maindotpy_file_loc, uppered_kwargs, eval_locals=__used_locals)
    for kwarg_name, kwarg_value in maindotpy_found_kwargs.items():
      if kwarg_name not in found_kwargs or found_kwargs[kwarg_name] is Parameter.empty or found_kwargs[kwarg_name] is None:
        found_kwargs[kwarg_name] = kwarg_value

  if "rich_console" in found_kwargs:
    rich_shared_console = get_console()

    if isinstance(found_console := found_kwargs.get("rich_console"), Console):
      rich_shared_console.__dict__ = found_console.__dict__

    elif found_console is not None:
      raise TypeError(f"Expected 'rich_console' to be of type Console, but got {type(found_console)}")

    found_kwargs["rich_console"] = rich_shared_console

  if any(arg is Parameter.empty for arg in found_kwargs.values()):
    missing_args = [name for name, value in found_kwargs.items() if value is Parameter.empty]
    raise ValueError(f"Missing required logging configuration arguments: {', '.join(missing_args)}")

  func_target(**found_kwargs)
  __initialized = True


def init_logging(*queues: QueueCatchall) -> None:
  """
  Initializes logging for the entire project. This should be called at the very beginning of the main entrypoint of the application.
  It will attempt to find any uppercase constants defined in __main__ that match the parameter names of the configure_logging function,
  and use those values to configure logging.\n
  If the expected constants are not found in __main__, it will attempt to fall back to the app's dedicated entrypoint script __main__.py
  to find those constants.\n
  This allows for flexible configuration of logging behavior without requiring changes to this module or the logging_config module.
  """
  try:
    logging_module = cast("config", import_module("logging_config"))
    configure_logging_main = logging_module.configure_logging_main
  except ImportError, AttributeError:
    # First party imports
    from sft_ext.logging.config import configure_logging_main

  __init_logging_base(queues, func_target=configure_logging_main)


def init_logging_worker(queue: QueueCatchall) -> None:
  """
  Handles the initialization of logging for worker processes.
  It will attempt to find any uppercase constants defined in __main__ that match the parameter names of the configure_logging function,
  and use those values to configure logging.\n
  If the expected constants are not found in __main__, it will attempt to fall back to the app's dedicated entrypoint script __main__.py
  to find those constants.\n
  This allows for flexible configuration of logging behavior without requiring changes to this module or the logging_config module.
  """
  try:
    logging_module = cast("config", import_module("logging_config"))
    configure_logging_worker = logging_module.configure_logging_worker
  except ImportError, AttributeError:
    # First party imports
    from sft_ext.logging.config import configure_logging_worker

  __init_logging_base(queue, func_target=configure_logging_worker)
