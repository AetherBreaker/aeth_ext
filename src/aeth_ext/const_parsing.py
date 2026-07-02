# Standard library imports
import ast
from pathlib import Path
from sys import argv, modules
from typing import TYPE_CHECKING, Any, TypeGuard

# First party imports
from aeth_ext.subclass_searchengine import get_entrypoint_root

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator

__all__ = ["parse_and_grab_constants"]


def __evaluate_constant_node(node: ast.Assign, source_code: str, eval_locals: dict[str, Any]) -> Any:
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
  return eval(expression_source, {"__builtins__": __builtins__}, eval_locals)


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


first_package_root = Path(get_entrypoint_root())
second_package_root = Path(get_entrypoint_root(argv[0]))

DEFAULT_SEARCH_PATHS: tuple[Path, ...] = (
  first_package_root / "__init__.py",
  second_package_root / "__init__.py",
  first_package_root / "__main__.py",
  second_package_root / "__main__.py",
  Path(modules["__main__"].__file__),  # pyright: ignore[reportArgumentType]
)


def parse_and_grab_constants(
  *fps: Path,
  expected_constants: dict[str, str],
  eval_locals: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """
  Parses Python source files via AST to locate and evaluate ALL_CAPS constant
  assignments, returning their values keyed by caller-supplied names.

  For each file (in order), the AST is walked for top-level ``ast.Assign``
  nodes whose targets are ALL-CAPS names. Assignments inside an
  ``if __name__ == "__main__":`` block are also included. When a matching name
  is found it is evaluated with ``eval_locals`` as the available namespace and
  stored in the result dict.

  Args:
    *fps: One or more ``Path`` objects to inspect. Files that do not exist or
      are not regular files are silently skipped. When omitted,
      ``DEFAULT_SEARCH_PATHS`` is iterated in reverse order so that earlier
      entries overwrite later ones — giving the following preference (highest
      to lowest):

      1. ``<entrypoint package root>/__init__.py``
      2. ``<argv[0] package root>/__init__.py``
      3. ``<entrypoint package root>/__main__.py``
      4. ``<argv[0] package root>/__main__.py``
      5. ``modules["__main__"].__file__`` — the currently executing module.

      Both package roots are the top-most package directory of the program
      entrypoint, resolved via ``get_entrypoint_root``. The ``argv[0]``
      variant is an explicit fallback for environments where
      ``__main__.__file__`` is unavailable (e.g. multiprocessing workers).
    expected_constants: A mapping from constant name (case-insensitive; stored
      keys are automatically uppercased) to the desired key name in the
      returned dict. Only constants whose names appear in this mapping are
      extracted.
    eval_locals: A namespace dict passed as *locals* when evaluating each
      constant's value expression. Use this to supply any names that the
      expression may reference (e.g. ``Path``, ``os``, helper callables).

  Returns:
    A ``dict`` whose keys are the values from ``expected_constants`` and whose
    values are the evaluated results of the corresponding constant expressions.
    Constants not found in any of the searched files are absent from the dict.
  """

  if eval_locals is None:
    eval_locals = {}
  results = {}

  it = iter(fps)

  if not fps:
    it = reversed(DEFAULT_SEARCH_PATHS)

  for fp in it:
    if not fp.exists() or not fp.is_file():
      continue

    main_file_text = fp.read_text()
    tree = ast.parse(main_file_text)

    # ensure keys in expected_constants are uppered
    expected_constants = {k.upper(): v for k, v in expected_constants.items()}

    for node, target in __yield_constant_assignments(tree.body):
      if isinstance(target, ast.Name) and target.id in expected_constants:
        actual_kwarg_name = expected_constants[target.id]
        value = __evaluate_constant_node(node, main_file_text, eval_locals)
        results[actual_kwarg_name] = value
  return results
