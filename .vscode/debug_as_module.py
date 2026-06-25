"""Debug bootstrap: run the file given as argv[1] as a module (``python -m``).

VS Code has no variable that turns a file path into a dotted module name, so
debugging ``${file}`` directly forces ``program`` mode, which makes debugpy
insert the script's own directory at ``sys.path[0]`` and shadow stdlib packages
(e.g. a local ``logging/`` package). This wrapper computes the dotted module
name by walking up while ``__init__.py`` exists, then executes it with runpy so
the package root (already on ``PYTHONPATH``) is used for imports.
"""

# Standard library imports
import runpy
import sys
from pathlib import Path


def module_name(file: Path) -> str:
  pkg = file.resolve().with_suffix("")
  parts = [pkg.name]
  parent = pkg.parent
  while (parent / "__init__.py").exists():
    parts.append(parent.name)
    parent = parent.parent
  return ".".join(reversed(parts))


def main() -> None:
  if len(sys.argv) < 2:  # noqa: PLR2004
    raise SystemExit("usage: debug_as_module.py <file> [args...]")

  target = Path(sys.argv[1])
  module = module_name(target)

  # Present argv to the target as if it were launched with ``python -m module``.
  sys.argv = [str(target.resolve()), *sys.argv[2:]]
  runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
  main()
