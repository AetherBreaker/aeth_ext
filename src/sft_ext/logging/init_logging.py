from annotationlib import Format
import ast
import sys
from contextlib import suppress
from importlib import import_module
from inspect import Parameter, signature
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.console import Console

from rich import get_console

if TYPE_CHECKING:
  from collections.abc import Callable, Iterator
  from typing import Any, TypeGuard

  from sft_ext.logging import logging_config
  from sft_ext.logging.logging_config import QueueCatchall


def __evaluate_constant_node(node: ast.Assign, source_code: str) -> Any:
  """
  Evaluates a specific AST node within a namespace populated
  only by the explicitly allowed imports.
  """
  # 1. Reconstruct the exact text snippet for the value expression
  # ast.get_source_segment was added in Python 3.8
  expression_source = ast.get_source_segment(source_code, node.value)
  if expression_source is None:
    raise ValueError("Could not extract source segment for the given AST node.")

  # 3. Evaluate the expression safely in that sandbox
  return eval(expression_source, {"__builtins__": __builtins__}, {"sys": sys, "platform": sys.platform, "Console": Console})


def __is_main_block(node: ast.stmt) -> TypeGuard[ast.If]:
  """
  Checks if an AST node is an 'if __name__ == "__main__":' statement.
  """
  if not isinstance(node, ast.If):
    return False

  # Check for: name == "string"
  if isinstance(node.test, ast.Compare):
    left = node.test.left
    # Must be comparing the variable '__name__'
    if isinstance(left, ast.Name) and left.id == "__name__" and (len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)):
      right = node.test.comparators[0]
      # Must be comparing against "__main__"
      if isinstance(right, ast.Constant) and right.value == "__main__":
        return True
  return False


def __yield_constant_assignments(nodes: list[ast.stmt]) -> Iterator[tuple[ast.Assign, ast.expr]]:
  for node in nodes:
    if isinstance(node, ast.Assign):
      for target in node.targets:
        if isinstance(target, ast.Name) and target.id.isupper():
          yield node, target
    elif __is_main_block(node):
      yield from __yield_constant_assignments(node.body)


def __parse_and_grab_constants(fp: Path, expected_constants: dict[str, str]) -> dict[str, Any]:
  results = {}
  main_file_text = fp.read_text()
  tree = ast.parse(main_file_text)

  for node, target in __yield_constant_assignments(tree.body):
    if isinstance(target, ast.Name) and target.id in expected_constants:
      actual_kwarg_name = expected_constants[target.id]
      value = __evaluate_constant_node(node, main_file_text)
      results[actual_kwarg_name] = value
  return results


__initialized = False


def __init_logging_base(queues: QueueCatchall | tuple[QueueCatchall, ...], func_target: Callable) -> None:  # noqa: C901, PLR0912
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

    main_module_found_kwargs = __parse_and_grab_constants(main_module_file_loc, uppered_kwargs)

    for kwarg_name, kwarg_value in main_module_found_kwargs.items():
      if kwarg_name not in found_kwargs or found_kwargs[kwarg_name] is Parameter.empty or found_kwargs[kwarg_name] is None:
        found_kwargs[kwarg_name] = kwarg_value

  maindotpy_file_loc = Path(sys.argv[0]).resolve()
  if maindotpy_file_loc.is_dir():
    maindotpy_file_loc = maindotpy_file_loc / "__main__.py"
  elif maindotpy_file_loc.name != "__main__.py":
    maindotpy_file_loc = maindotpy_file_loc.parent / "__main__.py"

  if (
    maindotpy_file_loc.exists()
    and str(main_module_file_loc) != str(maindotpy_file_loc)
    and any(value is None for value in found_kwargs.values())
  ):
    maindotpy_found_kwargs = __parse_and_grab_constants(maindotpy_file_loc, uppered_kwargs)
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
    logging_module = cast("logging_config", import_module("logging_config"))
    configure_logging_main = logging_module.configure_logging_main
  except ImportError, AttributeError:
    from sft_ext.logging.logging_config import configure_logging_main

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
    logging_module = cast("logging_config", import_module("logging_config"))
    configure_logging_worker = logging_module.configure_logging_worker
  except ImportError, AttributeError:
    from sft_ext.logging.logging_config import configure_logging_worker

  __init_logging_base(queue, func_target=configure_logging_worker)
