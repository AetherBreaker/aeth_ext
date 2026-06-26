# Standard library imports
import ast
from typing import TYPE_CHECKING, Any, TypeGuard

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator
  from pathlib import Path

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


def parse_and_grab_constants(fp: Path, expected_constants: dict[str, str], eval_locals: dict[str, Any]) -> dict[str, Any]:
  results = {}
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
