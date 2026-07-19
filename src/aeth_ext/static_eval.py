# This file was mostly AI generated.

# Standard library imports
import ast
import inspect
import warnings
from collections.abc import Iterator
from importlib import import_module
from logging import getLogger
from os import PathLike, fspath, scandir
from os.path import abspath, basename, dirname, isdir, isfile, join, splitext
from pathlib import Path
from sys import argv, modules
from typing import TYPE_CHECKING, Any, NamedTuple, TypeGuard

# Third party imports
from aiologic import Lock

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterable, Iterator

  type StrPath = str | PathLike[str]

logger = getLogger(__name__)

__all__ = [
  "SubclassInfo",
  "find_subclasses_local",
  "get_caller_file",
  "get_entrypoint_root",
  "get_package_root",
  "parse_and_grab_constants",
  "reset_subclass_caches",
]


# Directories that are never worth descending into when scanning a project's own
# source tree. Pruning these during the walk is the single biggest speed win, so
# the set is kept broad and matched by exact directory name.
DEFAULT_IGNORED_DIRS = frozenset(
  {
    ".bzr",
    ".eggs",
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
  }
)


class SubclassInfo(NamedTuple):
  """
  A statically discovered class definition.

  The class is described purely from source; nothing is imported until
  :py:meth:`load` is called.

  :ivar qualname:
      Fully qualified dotted path, e.g. ``"pkg.mod.Outer.Inner"``.
  :ivar name:
      The bare class name, e.g. ``"Inner"``.
  :ivar module:
      The importable module name, e.g. ``"pkg.mod"``.
  :ivar file:
      Absolute path to the ``.py`` file that defines the class.
  :ivar lineno:
      1-based line number of the ``class`` statement.
  :ivar depth:
      Inheritance distance from the queried base class: ``0`` is the base
      itself, ``1`` an immediate subclass, ``2`` a subclass of a subclass, and
      so on. Set by :meth:`SubclassIndex.descendants` per query; defaults to
      ``0`` for the canonical entries held inside an index.
  :ivar locality:
      Directory-ancestry distance from the search's starting point, as set by
      :func:`find_subclasses_local`: ``0`` means the class was found directly
      in the caller's own directory, ``1`` in the immediate parent directory,
      and so on. Defaults to ``0`` and is left unset (always ``0``) by any
      search that is not locality-aware.
  """

  qualname: str
  name: str
  module: str
  file: str
  lineno: int
  depth: int = 0
  locality: int = 0

  def load(self) -> type:
    """
    Import the defining module and return the live class object.

    :return:
        The class object referred to by this descriptor.
    """

    obj: object = import_module(self.module)
    # qualname is "<module>.<a>.<b>..." -> walk the attribute path after the module.
    for part in self.qualname[len(self.module) + 1 :].split("."):
      obj = getattr(obj, part)
    if not isinstance(obj, type):
      raise TypeError(f"{self.qualname!r} did not resolve to a class (got {type(obj)!r}).")
    return obj


def _evaluate_constant_node(node: ast.Assign, source_code: str, eval_locals: dict[str, Any]) -> Any:
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


def _is_main_block(node: ast.stmt) -> TypeGuard[ast.If]:
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


def _yield_constant_assignments(nodes: list[ast.stmt]) -> Iterator[tuple[ast.Assign, ast.expr]]:
  for node in nodes:
    if isinstance(node, ast.Assign):
      for target in node.targets:
        if isinstance(target, ast.Name) and target.id.isupper():
          yield node, target
    elif _is_main_block(node):
      yield from _yield_constant_assignments(node.body)


def __parse_and_grab_constants(
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
    *fps: The files to inspect, in order. Files that do not exist or are not
      regular files are silently skipped. Callers needing a caller-relative
      search should go through :func:`parse_and_grab_constants` instead of
      calling this directly; it computes an appropriate ``fps`` sequence via
      :func:`_collect_ancestry_config_files`.
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

  # ensure keys in expected_constants are uppered
  expected_constants = {k.upper(): v for k, v in expected_constants.items()}

  for fp in fps:
    if not fp.exists() or not fp.is_file():
      continue

    main_file_text = fp.read_text()
    tree = ast.parse(main_file_text)

    for node, target in _yield_constant_assignments(tree.body):
      if isinstance(target, ast.Name) and target.id in expected_constants:
        actual_kwarg_name = expected_constants[target.id]
        value = _evaluate_constant_node(node, main_file_text, eval_locals)
        results[actual_kwarg_name] = value
  return results


def _format_call_stack() -> str:
  """
  Format the call stack as dot-separated module.class.function names.

  Skips the first two frames (this function and its direct caller) to show the
  stack that led to the actual caller.
  """
  stack = inspect.stack()
  # Skip frame 0 (__format_call_stack) and frame 1 (get_entrypoint_root)
  frames = stack[2:]
  parts = []
  for frame_info in frames[:5]:  # Limit to 5 frames for readability
    code = frame_info.frame.f_code
    module_name = frame_info.frame.f_globals.get("__name__", "?")
    function_name = code.co_name

    # Try to extract class name from self or cls
    class_name = None
    if "self" in frame_info.frame.f_locals:
      class_name = frame_info.frame.f_locals["self"].__class__.__name__
    elif "cls" in frame_info.frame.f_locals:
      obj = frame_info.frame.f_locals["cls"]
      class_name = obj.__name__ if isinstance(obj, type) else obj.__class__.__name__

    if class_name:
      parts.append(f"{module_name}.{class_name}.{function_name}")
    else:
      parts.append(f"{module_name}.{function_name}")

  return "/".join(parts) if parts else "(no stack)"


def _resolve_root_without_main_file() -> str:
  """
  Determine the entrypoint root directory when ``__main__.__file__`` is absent.

  Tries three strategies in order:

  1. ``__main__.__spec__.origin`` — populated when Python is invoked with ``-m``.
  2. ``importlib.util.find_spec`` on ``__spec__.parent`` / ``__spec__.name`` —
     handles launchers that set ``__spec__`` but leave ``origin`` as ``None``.
  3. ``sys.argv[0]`` — reliable for spawned
     :py:mod:`multiprocessing` / :py:class:`~concurrent.futures.ProcessPoolExecutor`
     workers where the parent's ``argv`` is inherited.

  :raises AttributeError: when none of the three strategies can produce a path.
  :return: Absolute directory path for the entrypoint.
  """
  main_module = modules.get("__main__")
  spec = getattr(main_module, "__spec__", None)

  # Strategy 1: __spec__.origin — set when Python runs with -m.
  spec_origin = getattr(spec, "origin", None)
  if spec_origin and isfile(spec_origin):
    return dirname(abspath(spec_origin))

  # Strategy 2: resolve the package via importlib from __spec__'s name.
  # Handles launchers that set __spec__ but leave origin=None.
  spec_name = getattr(spec, "parent", None) or getattr(spec, "name", None)
  if spec_name:
    # Standard library imports
    from importlib.util import find_spec as _find_spec

    try:
      found = _find_spec(spec_name)
      if found and found.origin and isfile(found.origin):
        return dirname(abspath(found.origin))
    except Exception:
      pass

  # Strategy 3: argv[0] — reliable for spawned multiprocessing workers
  # where the parent's sys.argv is preserved in the child.
  entrypoint = abspath(argv[0]) if argv and argv[0] else None
  if entrypoint is None:
    raise AttributeError("module '__main__' has no attribute '__file__' and sys.argv[0] is unavailable")
  return entrypoint if isdir(entrypoint) else dirname(entrypoint)


def get_entrypoint_root(main_file: str | None = None) -> str:
  """
  Return the path of the top-most package containing the entrypoint script.

  Starting from the ``__main__`` module's file, this walks upward as long as each
  enclosing directory is a package (contains an ``__init__.py``) and returns the
  highest such package directory. When the entrypoint is a standalone script that
  is not part of any package, the directory holding it is returned.

  The walk stops early at a directory whose ``__main__.py`` exists and whose
  ``__main__.py``/``__init__.py`` does *not* set ``SKIP_ENTRYPOINT_MARKER = True``:
  that marks the directory as a directly-runnable package, making it the natural
  boundary rather than a mere namespace component that happens to be part of a
  larger package. A directory opts out of being treated as this boundary by
  setting the marker, letting the walk keep climbing towards a more meaningful
  ancestor -- see ``central_log_server/web_viewer/__main__.py``, which sets the
  marker so that its own directory is skipped in favour of ``central_log_server/``.

  This is the **ceiling** used by :func:`parse_and_grab_constants` for its
  caller-to-entrypoint ancestry walk. It is *not* used by the subclass search
  (:func:`find_subclasses_local`), which stops at :func:`get_package_root` instead.

  When the ``__main__`` module has no ``__file__`` -- as in a spawned
  :py:class:`~concurrent.futures.ProcessPoolExecutor`/:py:mod:`multiprocessing`
  worker whose ``__main__`` is the bootstrap module -- this falls back to
  ``sys.__spec__.origin``, then ``importlib.util.find_spec``, then
  ``sys.argv[0]``.  Running under the interactive interpreter (where none of
  these is available) is not supported and will raise :py:class:`AttributeError`.

  .. important::

      ``main_file`` is intentionally **not** evaluated at function-definition
      time.  This module is often imported as a side-effect of a parent-package
      ``__init__.py`` loaded by :mod:`runpy` *before* ``sys.modules["__main__"]``
      is updated to the real entry module.  Reading ``__main__`` at *call* time
      ensures the correct module is observed.

  :return:
      Absolute path of the top-most package directory, or the entrypoint's own
      directory when it is not packaged.
  """

  # Resolve the entry file at call time so we always see the fully-initialised
  # __main__ rather than the runpy bootstrap that was current at import time.
  if main_file is None:
    main_file = getattr(modules.get("__main__"), "__file__", None)

  root = dirname(abspath(main_file)) if main_file is not None else _resolve_root_without_main_file()

  while _is_package(root):
    if isfile(join(root, "__main__.py")) and not _dir_flag(root, "SKIP_ENTRYPOINT_MARKER"):
      break
    parent = _package_climb_step(root)
    if parent is None:
      break
    root = parent

  return root


def _package_climb_step(directory: str) -> str | None:
  """
  Return ``directory``'s parent if both it and the parent are packages (each
  contains an ``__init__.py``), else ``None``.

  ``None`` means ``directory`` is either the top of its contiguous package
  chain or the filesystem root -- there is nowhere further up worth climbing
  to. Shared by every upward walk in this module (:func:`get_package_root`,
  :func:`get_entrypoint_root`, :func:`_walk_ancestry`) so they all agree on
  what "up one package level" means.
  """
  if not _is_package(directory):
    return None
  parent = dirname(directory)
  if parent == directory or not _is_package(parent):
    return None
  return parent


def get_package_root(anchor_file: str | None = None) -> str:
  """
  Return the absolute top of the package containing ``anchor_file``.

  This is the generic "top of the package" primitive that both the subclass
  search and the other root helpers are built from: the widest directory
  reachable from ``anchor_file`` by climbing through successive ``__init__.py``
  parents. Unlike :func:`get_entrypoint_root`, it has no ``__main__.py``
  boundary logic -- it always climbs as far as the package chain allows.

  When ``anchor_file`` lives inside a ``site-packages`` directory (i.e. it is
  part of an installed package), the climb is short-circuited: the result is
  computed directly as ``<site-packages dir>/<top-level package name>``, scoped
  to that one top-level package so unrelated installed packages at the same
  level are never considered. This also sidesteps any ambiguity from namespace
  packages (no ``__init__.py`` at all), since the top-level name is derived from
  the module's own dotted path rather than from filesystem probing.

  :param anchor_file:
      A file whose enclosing package should be located. Defaults to this
      module's own ``__file__``.
  :return:
      Absolute path of the top-most package directory containing ``anchor_file``,
      or the directory containing it when it is not packaged at all.
  """
  anchor_path = abspath(anchor_file) if anchor_file is not None else abspath(__file__)
  anchor_parts = Path(anchor_path).parts

  if "site-packages" in anchor_parts:
    sp_idx = next(i for i, part in enumerate(anchor_parts) if part == "site-packages")
    site_packages_dir = Path(*anchor_parts[: sp_idx + 1])
    top_level_pkg = _module_qualname(anchor_path).split(".", 1)[0]
    return str(site_packages_dir / top_level_pkg)

  root = dirname(anchor_path)
  while (parent := _package_climb_step(root)) is not None:
    root = parent
  return root


def get_caller_file(stack_depth: int = 1) -> str | None:
  """
  Return the absolute file path of a frame above whatever function calls this.

  ``stack_depth=1`` (the default) returns the file of **the caller of the
  function that itself calls** :func:`get_caller_file` -- i.e. "my direct
  caller" from that intermediate function's own point of view. This is the
  shape every public API in this module wants: a wrapper auto-detects its own
  caller with a single call, ``caller_file = caller_file or get_caller_file(1)``,
  without needing to know its own name or position on the stack.

  Pass a larger value to deliberately skip additional layers of wrapping (e.g.
  a thin function that forwards to another helper which itself wants to know
  about *its* caller's caller).

  :param stack_depth:
      How many frames above the immediate caller of :func:`get_caller_file` to
      report. ``1`` means that caller's own caller (see above).
  :return:
      The absolute file path of the requested frame, or ``None`` if the stack
      is not deep enough or that frame has no backing source file (e.g. code
      typed at an interactive prompt).
  """
  stack = inspect.stack()
  target_index = 1 + stack_depth
  if target_index >= len(stack):
    return None
  filename = abspath(stack[target_index].filename)
  return filename if isfile(filename) else None


def _dir_flag(directory: str, constant_name: str) -> bool:
  """
  Return whether ``directory`` sets the ALL-CAPS constant ``constant_name`` to
  ``True`` in its ``__main__.py`` and/or ``__init__.py``.

  Both files are checked; if both define the constant, ``__init__.py``'s value
  wins, consistent with :func:`parse_and_grab_constants`'s own
  "prefer ``__init__.py``" rule. This one helper backs every skip-flag used by
  the ancestry walks in this module -- ``SKIP_ENTRYPOINT_MARKER``,
  ``SKIP_SUBCLASS_SEARCH``, and ``SKIP_CONSTANT_SEARCH`` -- so all three behave
  identically. Memoised per ``(directory, constant_name)`` for the life of the
  process, consistent with the scanner's file-immutability assumption.
  """

  def compute() -> bool:
    result = __parse_and_grab_constants(
      Path(join(directory, "__main__.py")),
      Path(join(directory, "__init__.py")),
      expected_constants={constant_name: "flag"},
    )
    return bool(result.get("flag", False))

  return _DIR_FLAG_CACHE.get_or_compute((directory, constant_name), compute)


def _walk_ancestry(start_dir: str, ceiling_dir: str) -> Iterator[tuple[str, int]]:
  """
  Yield ``(directory, depth)`` from ``start_dir`` (``depth=0``) upward through
  each successive package parent, stopping once ``ceiling_dir`` has been
  yielded.

  This is the one upward walk shared by both locality-aware searches in this
  module (:func:`_collect_ancestry_files` for subclasses,
  :func:`_collect_ancestry_config_files` for constants): it only ever climbs
  towards the filesystem root, never descends into a sibling or child
  directory, and always includes ``start_dir`` itself at ``depth=0``.

  If ``ceiling_dir`` is not actually an ancestor of ``start_dir`` (e.g.
  ``start_dir`` lives inside an installed package while ``ceiling_dir`` is a
  separate consuming application's root), the walk still terminates
  gracefully: it simply stops climbing once it runs out of enclosing packages,
  the same place :func:`get_package_root` would stop.
  """
  current = abspath(start_dir)
  ceiling = abspath(ceiling_dir)
  depth = 0
  while True:
    yield current, depth
    if current == ceiling:
      return
    parent = _package_climb_step(current)
    if parent is None:
      return
    current = parent
    depth += 1


def __scandir_direct(directory: str) -> tuple[str, ...]:
  """List the ``.py`` files directly inside ``directory`` (uncached, no recursion)."""
  try:
    scanner = scandir(directory)
  except NotADirectoryError, FileNotFoundError, PermissionError:
    return ()
  with scanner as entries:
    return tuple(entry.path for entry in entries if entry.name.endswith(".py") and not entry.is_dir(follow_symlinks=False))


def _list_direct_py_files(directory: str) -> tuple[str, ...]:
  """
  Return the ``.py`` files directly inside ``directory``, memoised for the life
  of the process. Never recurses -- this is the file-collection primitive for
  *every* level of a locality-aware subclass search, including the caller's own
  starting directory (subclass search never descends into subdirectories at
  any level, not even the first).
  """
  return _DIR_LISTING_CACHE.get_or_compute(directory, lambda: __scandir_direct(directory))


def _collect_ancestry_files(caller_file: str, ceiling_dir: str) -> dict[str, int]:
  """
  Map every ``.py`` file eligible for a locality-aware subclass search to its
  locality (ancestry depth), walking from ``caller_file``'s directory up to
  ``ceiling_dir``.

  Every level -- including the caller's own directory -- is scanned
  non-recursively (direct files only): subclass search never descends into
  subdirectories, so sibling packages and nested subpackages are never seen,
  even at the starting level. A level whose ``__main__.py``/``__init__.py``
  sets ``SKIP_SUBCLASS_SEARCH = True`` is omitted entirely, *unless* it is the
  ceiling itself (the ceiling always participates so the search can never
  silently come back with nothing at all).

  The result is fed straight into the subclass index builder, so only files
  within this ancestry window are ever parsed: the search is lazy and never
  indexes anything outside the caller's ``[start, ceiling]`` window.
  """
  files: dict[str, int] = {}
  start_dir = dirname(abspath(caller_file))
  ceiling = abspath(ceiling_dir)
  for directory, depth in _walk_ancestry(start_dir, ceiling):
    if directory != ceiling and _dir_flag(directory, "SKIP_SUBCLASS_SEARCH"):
      continue
    for path in _list_direct_py_files(directory):
      files.setdefault(path, depth)
  return files


def _collect_ancestry_config_files(caller_file: str, ceiling_dir: str) -> list[Path]:
  """
  Collect the ``__main__.py``/``__init__.py`` files eligible for a
  locality-aware constant search, walking from ``caller_file``'s directory up
  to ``ceiling_dir``.

  Within a single directory, ``__main__.py`` is listed before ``__init__.py``
  so that -- when both define the same constant -- ``__init__.py``'s value
  wins (:func:`__parse_and_grab_constants` keeps the *last* value seen for a
  given name). Across directories, farther levels are listed before closer
  ones, so once everything is unioned by that same last-value-wins rule, the
  caller's own directory always has the final say. A directory whose files set
  ``SKIP_CONSTANT_SEARCH = True`` is omitted entirely, *unless* it is the
  ceiling itself.

  ``ceiling_dir`` is not always a genuine ancestor of ``caller_file`` --
  :func:`parse_and_grab_constants` uses the process entrypoint
  (:func:`get_entrypoint_root`) as its ceiling, which is a global, caller-
  independent location. A shared library module whose own directory is a
  *sibling* of the entrypoint's package (rather than one of its ancestors) --
  e.g. a generic module reused by several different applications -- would
  otherwise never see the ceiling at all once the climb from its own directory
  runs out of enclosing packages. In that case the ceiling's own files are
  still unioned in, at the lowest priority (as if it were the farthest
  ancestor), so application-level constants defined only at the true
  entrypoint (e.g. ``PROJECT_NAME``) remain discoverable from anywhere.

  :return:
      Candidate paths ordered farthest-from-caller first, caller's own
      directory last. Nonexistent files are included;
      :func:`__parse_and_grab_constants` silently skips them.
  """
  start_dir = dirname(abspath(caller_file))
  ceiling = abspath(ceiling_dir)
  levels = sorted(_walk_ancestry(start_dir, ceiling), key=lambda pair: pair[1], reverse=True)

  ordered_files: list[Path] = []
  if levels[0][0] != ceiling:
    ordered_files.append(Path(join(ceiling, "__main__.py")))
    ordered_files.append(Path(join(ceiling, "__init__.py")))

  for directory, _depth in levels:
    if directory != ceiling and _dir_flag(directory, "SKIP_CONSTANT_SEARCH"):
      continue
    ordered_files.append(Path(join(directory, "__main__.py")))
    ordered_files.append(Path(join(directory, "__init__.py")))
  return ordered_files


def get_cls_scan_root(cls: type) -> str:
  """
  .. deprecated::
      Use :func:`get_package_root` instead (e.g.
      ``get_package_root(sys.modules[cls.__module__].__file__)``). Kept only as
      a thin backward-compatible wrapper for external callers that have not
      migrated yet.
  """
  warnings.warn(
    "get_cls_scan_root() is deprecated; use get_package_root() instead.",
    DeprecationWarning,
    stacklevel=2,
  )
  module = modules.get(cls.__module__)
  cls_file = getattr(module, "__file__", None)
  if cls_file is None:
    return get_entrypoint_root()
  return get_package_root(cls_file)


def parse_and_grab_constants(
  expected_constants: dict[str, str],
  *,
  caller_file: str | None = None,
  eval_locals: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """
  Parse ALL-CAPS constants out of the caller's own package ancestry.

  Walks upward from ``caller_file``'s directory (or, when omitted, the direct
  caller of this function) to :func:`get_entrypoint_root`, reading each
  level's ``__main__.py`` and ``__init__.py`` (``__init__.py`` wins on a
  conflict within the same directory). A value found in a directory closer to
  the caller takes precedence over the same name found farther away, and
  distinct names found at different levels are unioned together. A directory
  can exclude itself from the search by setting ``SKIP_CONSTANT_SEARCH = True``
  in either file -- unless that directory is the entrypoint ceiling itself, in
  which case the flag is ignored so the search never comes back with nothing
  at all.

  :param expected_constants:
      A mapping from constant name (case-insensitive; stored keys are
      automatically uppercased) to the desired key name in the returned dict.
      Only constants whose names appear in this mapping are extracted.
  :param caller_file:
      The file to treat as the search's starting point. Defaults to the direct
      caller of this function (via :func:`get_caller_file`); pass this
      explicitly when the natural call stack does not reflect the real
      starting point (e.g. a wrapper that itself wants to search from *its*
      caller's location rather than its own).
  :param eval_locals:
      A namespace dict passed as *locals* when evaluating each constant's value
      expression. Use this to supply any names that the expression may
      reference (e.g. ``Path``, ``os``, helper callables).
  :return:
      A ``dict`` whose keys are the values from ``expected_constants`` and whose
      values are the evaluated results of the corresponding constant expressions.
      Constants not found anywhere in the searched ancestry are absent from the
      dict.
  """
  if caller_file is None:
    caller_file = get_caller_file(1)
    if caller_file is None:
      raise RuntimeError("parse_and_grab_constants: could not automatically determine the calling file; pass caller_file explicitly.")

  ceiling_dir = get_entrypoint_root()
  fps = _collect_ancestry_config_files(caller_file, ceiling_dir)

  logger.debug(
    "parse_and_grab_constants searching %d path(s) for constants %r:\n  %s",
    len(fps),
    list(expected_constants.keys()),
    "\n  ".join(str(p) for p in fps),
  )
  return __parse_and_grab_constants(*fps, expected_constants=expected_constants, eval_locals=eval_locals)


class _RawClass(NamedTuple):
  """A class definition captured before its bases have been resolved."""

  qualname: str
  name: str
  module: str
  file: str
  lineno: int
  bases: tuple[str, ...]


class _FileRecord(NamedTuple):
  """Everything extracted from a single source file, cached by ``mtime``/size."""

  module: str
  imports: dict[str, str]
  classes: tuple[_RawClass, ...]
  top_level: frozenset[str]


# A class paired with its resolved base edges: (raw class, base qualnames, base bare names).
type _ResolvedClass = tuple[_RawClass, frozenset[str], frozenset[str]]


# --- Process-wide caches -------------------------------------------------------
# Scanned files are assumed immutable for the lifetime of the process (no edits,
# no new files), so every cache below is keyed purely by content identity and is
# never invalidated. Work is therefore shared across calls and across overlapping
# ``roots``: each directory is walked once, each file is read/parsed/indexed once,
# and identical file sets reuse a fully prebuilt index. Call
# :func:`reset_subclass_caches` if that immutability assumption is ever broken.


class _OnceCache[K, V]:
  """
  A thread-safe cache that computes each key's value *exactly once*.

  Concurrent callers requesting the same missing key elect a single thread to run
  the (potentially expensive) computation; the rest block on a per-key lock and,
  once it completes, all observe the one stored result. Distinct keys never block
  one another, so unrelated files are still walked, parsed and indexed in
  parallel. Warm lookups take a lock-free fast path.

  Nesting is safe: caches are consulted in a strict layered order
  (index -> edges -> record -> package/walk), so a computation may consult
  lower-layer caches while holding its own key lock without risk of deadlock.
  """

  __slots__ = ("_guard", "_key_locks", "_values")

  _values: dict[K, V]
  _key_locks: dict[K, Lock]
  _guard: Lock

  def __init__(self) -> None:
    super().__init__()
    self._values = {}
    self._key_locks = {}
    self._guard = Lock()

  def get_or_compute(self, key: K, compute: Callable[[], V]) -> V:
    """Return the cached value for ``key``, computing it once if absent."""

    values = self._values

    # Lock-free fast path: once written, a key's value is never mutated, and dict
    # reads are safe under both GIL and free-threaded builds.
    try:
      return values[key]
    except KeyError:
      pass

    # Elect (or join) the single computing thread for this key.
    with self._guard:
      try:
        return values[key]
      except KeyError:
        pass
      key_lock = self._key_locks.get(key)
      if key_lock is None:
        key_lock = Lock()
        self._key_locks[key] = key_lock

    with key_lock:
      # A prior holder of this key lock may have just computed the value.
      with self._guard:
        try:
          return values[key]
        except KeyError:
          pass

      value = compute()

      with self._guard:
        values[key] = value
        # First successful computation: retire the key lock so future lookups
        # stay on the lock-free fast path and the registry cannot grow unbounded.
        self._key_locks.pop(key, None)
      return value

  def clear(self) -> None:
    """Forget every cached value and pending key lock."""

    with self._guard:
      self._values.clear()
      self._key_locks.clear()


# Directory ``(root, ignored_dirs)`` -> tuple of contained ``.py`` file paths.
_WALK_CACHE = _OnceCache[tuple[str, frozenset[str]], tuple[str, ...]]()

# Directory path -> whether it is a package (memoises ``__init__.py`` probing).
_PKG_CACHE = _OnceCache[str, bool]()

# File path -> parsed record (``None`` marks an unreadable/unparseable file).
_RECORD_CACHE = _OnceCache[str, _FileRecord | None]()

# File path -> that file's classes paired with their resolved base edges.
_EDGES_CACHE = _OnceCache[str, tuple[_ResolvedClass, ...]]()

# (directory, ALL-CAPS constant name) -> whether that directory's __main__.py /
# __init__.py sets it to True. Backs SKIP_ENTRYPOINT_MARKER, SKIP_SUBCLASS_SEARCH
# and SKIP_CONSTANT_SEARCH.
_DIR_FLAG_CACHE = _OnceCache[tuple[str, str], bool]()

# Directory path -> the .py files directly inside it (no recursion). Used by
# every level of a locality-aware ancestry walk, including the caller's own
# starting directory.
_DIR_LISTING_CACHE = _OnceCache[str, tuple[str, ...]]()

# Frozen set of file paths -> the index assembled from exactly those files
# (instantiated after ``SubclassIndex`` is defined, below).


def reset_subclass_caches() -> None:
  """
  Clear every process-wide cache used by the subclass scanner.

  The scanner assumes scanned files never change. Call this only if that
  assumption is deliberately broken (e.g. between tests that rewrite files on
  disk) to force a full re-scan on the next call. Each cache is cleared
  independently; the clears are not atomic as a group, which is fine for a
  maintenance operation that should not race with live scans.
  """

  _WALK_CACHE.clear()
  _PKG_CACHE.clear()
  _RECORD_CACHE.clear()
  _EDGES_CACHE.clear()
  _DIR_FLAG_CACHE.clear()
  _DIR_LISTING_CACHE.clear()
  _INDEX_CACHE.clear()


def _normalize_roots(roots: Iterable[StrPath] | StrPath) -> list[str]:
  """Coerce a single path or an iterable of paths into a list of absolute paths."""

  if isinstance(roots, (str, PathLike)):
    return [abspath(fspath(roots))]
  return [abspath(fspath(r)) for r in roots]


def __scandir_walk(root: str, ignored_dirs: frozenset[str]) -> tuple[str, ...]:
  """Walk a single absolute ``root`` and return its ``.py`` files (uncached)."""

  files: list[str] = []
  stack = [root]
  while stack:
    current = stack.pop()
    try:
      scanner = scandir(current)
    except NotADirectoryError, FileNotFoundError, PermissionError:
      continue
    with scanner as entries:
      for entry in entries:
        if entry.is_dir(follow_symlinks=False):
          if entry.name not in ignored_dirs:
            stack.append(entry.path)
        elif entry.name.endswith(".py"):
          files.append(entry.path)
  return tuple(files)


def _walk_root(root: str, ignored_dirs: frozenset[str]) -> tuple[str, ...]:
  """Return the cached ``.py`` file listing for a single ``root`` directory."""

  key = (root, ignored_dirs)
  return _WALK_CACHE.get_or_compute(key, lambda: __scandir_walk(root, ignored_dirs))


def _iter_python_files(
  roots: Iterable[StrPath] | StrPath,
  *,
  ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
) -> Iterator[str]:
  """
  Yield the absolute path of every ``.py`` file beneath ``roots``.

  Uses :py:func:`os.scandir` and prunes ``ignored_dirs`` for speed; symbolic
  links to directories are not followed to avoid cycles. Each directory is walked
  at most once per process and its listing cached, so repeated or overlapping
  ``roots`` reuse earlier work; a file reachable from more than one root is
  yielded only once.

  :param roots:
      A single path or an iterable of paths to walk.
  :param ignored_dirs:
      Directory names to skip entirely.

  :return:
      An iterator of absolute file paths.
  """

  seen: set[str] = set()
  for root in _normalize_roots(roots):
    for path in _walk_root(root, ignored_dirs):
      if path not in seen:
        seen.add(path)
        yield path


def _is_package(directory: str) -> bool:
  """Return whether ``directory`` is a package, memoised exactly once."""

  return _PKG_CACHE.get_or_compute(directory, lambda: isfile(join(directory, "__init__.py")))


def _module_qualname(path: str) -> str:
  """
  Derive the dotted module name for ``path`` from its package layout.

  Walks upward while each parent directory is a package (contains an
  ``__init__.py``); the package test is memoised across the whole scan.
  """

  name = splitext(basename(path))[0]
  parts = [] if name == "__init__" else [name]

  directory = dirname(path)
  while _is_package(directory):
    parts.append(basename(directory))
    directory = dirname(directory)

  parts.reverse()
  return ".".join(parts)


def _dotted_name(node: ast.expr) -> str | None:
  """
  Render a base-class expression to a dotted name.

  Handles ``Name`` and ``Attribute`` chains and unwraps ``Subscript`` (e.g.
  ``Generic[T]`` -> ``Generic``). Returns ``None`` for anything else (calls,
  conditional expressions, etc.).
  """

  match node:
    case ast.Name(id=identifier):
      return identifier
    case ast.Attribute(value=value, attr=attr):
      prefix = _dotted_name(value)
      return f"{prefix}.{attr}" if prefix is not None else None
    case ast.Subscript(value=value):
      return _dotted_name(value)
    case _:
      return None


def _resolve_from(node: ast.ImportFrom, package: str) -> str:
  """Resolve the absolute target module of a ``from ... import ...`` statement."""

  if not node.level:
    return node.module or ""

  base = package.split(".") if package else []
  ascend = node.level - 1
  if ascend:
    base = base[:-ascend] if ascend <= len(base) else []

  if node.module:
    base.append(node.module)
  return ".".join(base)


def _iter_nested_blocks(node: ast.stmt) -> Iterator[list[ast.stmt]]:
  """Yield the statement blocks nested inside an ``if`` or ``try`` statement."""

  match node:
    case ast.If(body=body, orelse=orelse):
      yield body
      yield orelse
    case ast.Try(body=body, orelse=orelse, finalbody=finalbody, handlers=handlers):
      yield body
      yield orelse
      yield finalbody
      for handler in handlers:
        yield handler.body
    case _:
      pass


def _record_import(node: ast.Import, table: dict[str, str]) -> None:
  """Record bindings introduced by a plain ``import a.b.c [as x]`` statement."""

  for alias in node.names:
    if alias.asname:
      table[alias.asname] = alias.name
    else:
      # ``import a.b.c`` binds the top name ``a`` in the namespace.
      top = alias.name.partition(".")[0]
      table[top] = top


def _record_import_from(node: ast.ImportFrom, package: str, table: dict[str, str]) -> None:
  """Record bindings introduced by a ``from ... import ...`` statement."""

  target = _resolve_from(node, package)
  for alias in node.names:
    if alias.name == "*":
      continue
    local = alias.asname or alias.name
    table[local] = f"{target}.{alias.name}" if target else alias.name


def _collect_imports(body: list[ast.stmt], package: str, table: dict[str, str]) -> None:
  """
  Populate ``table`` mapping each locally bound name to its absolute target.

  Descends into ``if``/``try`` blocks so conditional imports are still resolved.
  """

  for node in body:
    if isinstance(node, ast.Import):
      _record_import(node, table)
    elif isinstance(node, ast.ImportFrom):
      _record_import_from(node, package, table)
    else:
      for block in _iter_nested_blocks(node):
        _collect_imports(block, package, table)


def _collect_classes(body: list[ast.stmt], enclosing: str, module: str, file: str, out: list[_RawClass]) -> None:
  """
  Append a :class:`__RawClass` for every class defined in ``body``.

  Descends into nested classes and ``if``/``try`` blocks but deliberately skips
  function bodies: classes defined inside functions cannot be reached by import
  alone, and skipping those subtrees keeps the walk tight.
  """

  for node in body:
    if isinstance(node, ast.ClassDef):
      qualname = f"{enclosing}.{node.name}"
      base_names = tuple(dotted for base in node.bases if (dotted := _dotted_name(base)) is not None)
      out.append(_RawClass(qualname, node.name, module, file, node.lineno, base_names))
      _collect_classes(node.body, qualname, module, file, out)
    else:
      for block in _iter_nested_blocks(node):
        _collect_classes(block, enclosing, module, file, out)


def _do_parse_file(path: str) -> _FileRecord | None:
  """Read and parse ``path`` into a :class:`__FileRecord` (uncached)."""

  try:
    with open(path, "rb") as handle:
      source = handle.read()
  except OSError as exc:
    logger.debug("Skipping unreadable file %s: %s", path, exc)
    return None

  # A ``class`` statement is the only way to define a class, so a file whose
  # bytes never contain that keyword cannot contribute anything to the index.
  # ``ast.parse`` dominates a cold scan (~85% of its time), so cheaply skipping
  # it for class-free modules is the single biggest safe win. ``class`` is ASCII,
  # making the byte test sound for the UTF-8/ASCII source this scanner targets; a
  # stray match (e.g. ``classmethod`` or a comment) only costs a needless parse,
  # never a missed class.
  if b"class" not in source:
    return None

  try:
    tree = ast.parse(source, filename=path)
  except (SyntaxError, ValueError) as exc:
    logger.debug("Skipping unparseable file %s: %s", path, exc)
    return None

  module = _module_qualname(path)
  package = module if basename(path) == "__init__.py" else module.rpartition(".")[0]

  imports: dict[str, str] = {}
  _collect_imports(tree.body, package, imports)

  classes: list[_RawClass] = []
  _collect_classes(tree.body, module, module, path, classes)

  top_level = frozenset(cls.name for cls in classes if cls.qualname == f"{module}.{cls.name}")
  return _FileRecord(module, imports, tuple(classes), top_level)


def _parse_file(path: str) -> _FileRecord | None:
  """
  Parse ``path`` into a :class:`__FileRecord`, memoised for the whole process.

  Files are assumed never to change, so a path is read and parsed exactly once
  (concurrent callers wait on the one in-flight parse); the result, including
  ``None`` for an unreadable or unparseable file, is cached. A single bad file
  never aborts a whole-tree scan.
  """

  return _RECORD_CACHE.get_or_compute(path, lambda: _do_parse_file(path))


def _compute_edges(path: str) -> tuple[_ResolvedClass, ...]:
  """Resolve ``path``'s classes to their base edges (uncached)."""

  record = _parse_file(path)
  if record is None:
    return ()

  return tuple(
    (
      cls,
      frozenset(_resolve_base(base, record.imports, record.top_level, record.module) for base in cls.bases),
      frozenset(base.rpartition(".")[2] for base in cls.bases),
    )
    for cls in record.classes
  )


def _index_file(path: str) -> tuple[_ResolvedClass, ...]:
  """
  Return ``path``'s classes paired with their resolved base edges, memoised.

  Base resolution depends only on the file's own imports and contents, so the
  result is stable per file and computed exactly once. This is the per-file
  "index" step shared across every :func:`build_subclass_index` call, including
  overlapping ``roots``.
  """

  return _EDGES_CACHE.get_or_compute(path, lambda: _compute_edges(path))


def _resolve_base(dotted: str, imports: dict[str, str], top_level: frozenset[str], module: str) -> str:
  """
  Resolve a base-class reference to its best-effort absolute dotted name.

  Resolution order mirrors Python's own name lookup:

  1. Longest matching prefix in the file's import table (handles aliases and
     ``import a.b.c`` style references).
  2. A class defined at the top level of the same module.
  3. The original text, returned unchanged as a last resort.
  """

  parts = dotted.split(".")
  for i in range(len(parts), 0, -1):
    prefix = ".".join(parts[:i])
    target = imports.get(prefix)
    if target is not None:
      rest = parts[i:]
      return ".".join((target, *rest)) if rest else target

  if parts[0] in top_level:
    return f"{module}.{dotted}"

  return dotted


class _SubclassIndex:
  """
  A reusable, statically built inheritance index.

  Build the index once with :func:`build_subclass_index` (or
  :func:`find_subclasses`) and query it for as many base classes as you like;
  the expensive parse happens a single time.
  """

  __slots__ = ("__by_qual", "__children_by_name", "__children_by_qual")

  def __init__(self, resolved: Iterable[_ResolvedClass]) -> None:
    super().__init__()
    by_qual: dict[str, SubclassInfo] = {}
    children_by_qual: dict[str, list[str]] = {}
    children_by_name: dict[str, list[str]] = {}

    for rec, base_quals, base_names in resolved:
      by_qual[rec.qualname] = SubclassInfo(rec.qualname, rec.name, rec.module, rec.file, rec.lineno)

      # Precise edges: every base resolved to an absolute dotted name.
      for base_qual in base_quals:
        children_by_qual.setdefault(base_qual, []).append(rec.qualname)
      # Fallback edges keyed by the base's bare name, used only to seed a query
      # from a base that lives outside the scanned tree (e.g. the library class).
      for base_name in base_names:
        children_by_name.setdefault(base_name, []).append(rec.qualname)

    self.__by_qual = by_qual
    self.__children_by_qual = children_by_qual
    self.__children_by_name = children_by_name

  @staticmethod
  def __depth_limit(recursive: bool | int) -> int | None:
    """
    Translate a ``recursive`` flag into a maximum search depth.

    ``bool`` is a subclass of ``int``, so the flags are distinguished by
    identity: ``True`` -> ``None`` (unlimited), ``False`` -> ``1`` (immediate
    subclasses only), any other integer is used verbatim as the depth cap.
    """

    if recursive is True:
      return None
    if recursive is False:
      return 1
    return recursive

  def __add_children(
    self,
    parents: Iterable[str],
    depth: int,
    result: dict[str, SubclassInfo],
  ) -> list[str]:
    """Tag undiscovered precise-edge children of ``parents`` at ``depth``."""

    discovered: list[str] = []
    for parent in parents:
      for child_qual in self.__children_by_qual.get(parent, ()):
        if child_qual != parent and child_qual not in result and (info := self.__by_qual.get(child_qual)) is not None:
          result[child_qual] = info._replace(depth=depth)
          discovered.append(child_qual)
    return discovered

  def descendants(
    self,
    base: type | str,
    *,
    include_name_fallback: bool = True,
    recursive: bool | int = True,
  ) -> tuple[SubclassInfo, ...]:
    """
    Return subclasses of ``base`` found during the scan, each tagged with depth.

    Subclasses are discovered through precise qualified-name edges. The query is
    *seeded* from ``base`` using both its qualified name and (optionally) its
    bare name, since the base class itself is usually defined outside the scanned
    tree and therefore cannot be matched by qualified name there.

    :param base:
        Either a live class object or a fully qualified ``"module.Qual"`` string.
    :param include_name_fallback:
        When ``True`` (default), also seed from subclasses whose base merely
        shares ``base``'s bare name. This catches subclasses whose imports could
        not be resolved statically, at the small risk of matching an unrelated
        class of the same name.
    :param recursive:
        Controls how deep the search descends. ``True`` (default) follows the
        inheritance chain to unlimited depth; ``False`` returns only immediate
        (depth ``1``) subclasses; an integer caps the search at that maximum
        depth (e.g. ``2`` includes immediate subclasses and their direct
        subclasses). A limit of ``0`` or less yields nothing. Note that
        ``recursive=1`` is equivalent to ``recursive=False``.

    :return:
        Discovered subclasses in discovery (breadth-first) order, excluding
        ``base`` itself. Each :class:`SubclassInfo` carries the ``depth`` at which
        it was found (``1`` for an immediate subclass). Under diamond inheritance
        a class is reported at its shallowest depth.
    """

    max_depth = self.__depth_limit(recursive)
    if max_depth is not None and max_depth <= 0:
      return ()

    if isinstance(base, type):
      target_qual = f"{base.__module__}.{base.__qualname__}"
      target_name = base.__name__
    else:
      target_qual = base
      target_name = base.rpartition(".")[2]

    result: dict[str, SubclassInfo] = {}

    # Seed depth 1: precise qualified-name edges plus optional bare-name edges.
    seeds = list(self.__children_by_qual.get(target_qual, ()))
    if include_name_fallback:
      precise = set(seeds)
      seeds.extend(qual for qual in self.__children_by_name.get(target_name, ()) if qual not in precise)

    frontier: list[str] = []
    for child_qual in seeds:
      if child_qual != target_qual and child_qual not in result and (info := self.__by_qual.get(child_qual)) is not None:
        result[child_qual] = info._replace(depth=1)
        frontier.append(child_qual)

    # Expand level by level using only precise qualified-name edges: in-tree
    # subclasses always resolve their bases to a known qualified name. Level
    # order assigns each class its shallowest depth under diamond inheritance.
    depth = 1
    while frontier and (max_depth is None or depth < max_depth):
      depth += 1
      frontier = self.__add_children(frontier, depth, result)

    return tuple(result.values())

  def all_classes(self) -> tuple[SubclassInfo, ...]:
    """Return every class discovered during the scan."""

    return tuple(self.__by_qual.values())


# Frozen set of file paths -> the index assembled from exactly those files.
_INDEX_CACHE = _OnceCache[frozenset[str], _SubclassIndex]()


def _build_subclass_index(
  roots: Iterable[StrPath] | StrPath,
  *,
  ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
) -> _SubclassIndex:
  """
  Scan ``roots`` and build a reusable :class:`SubclassIndex`.

  All work is memoised for the lifetime of the process: directory walks, file
  parsing, per-file base resolution and the assembled index are each cached, so
  repeated calls reuse earlier results and overlapping ``roots`` only ever parse
  and index files not already seen. The scanner assumes scanned files never
  change; use :func:`reset_subclass_caches` to force a fresh scan.

  :param roots:
      A single path or an iterable of paths to scan.
  :param ignored_dirs:
      Directory names to skip while walking.

  :return:
      An index that can be queried for any number of base classes.
  """

  files = frozenset(_iter_python_files(roots, ignored_dirs=ignored_dirs))

  def build() -> _SubclassIndex:
    resolved: list[_ResolvedClass] = []
    for path in files:
      resolved.extend(_index_file(path))
    return _SubclassIndex(resolved)

  return _INDEX_CACHE.get_or_compute(files, build)


def find_subclasses(
  base: type | str,
  roots: Iterable[StrPath] | StrPath,
  *,
  ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
  include_name_fallback: bool = False,
  recursive: bool | int = True,
) -> tuple[SubclassInfo, ...]:
  """
  Statically find subclasses of ``base`` beneath ``roots``, tagged with depth.

  No modules are imported. This sees subclasses whose defining modules have not
  been imported yet, which :py:meth:`type.__subclasses__` cannot.

  :param base:
      The base class (a live class object or ``"module.Qual"`` string).
  :param roots:
      A single path or an iterable of paths to scan.
  :param ignored_dirs:
      Directory names to skip while walking.
  :param include_name_fallback:
      When ``True`` (default), also seed from subclasses whose base merely
      shares ``base``'s bare name. This catches subclasses whose imports could
      not be resolved statically, at the small risk of matching an unrelated
      class of the same name.
  :param recursive:
      Controls how deep the search descends. ``True`` (default) follows the
      inheritance chain to unlimited depth; ``False`` returns only immediate
      (depth ``1``) subclasses; an integer caps the search at that maximum depth.
      A limit of ``0`` or less yields nothing.

  :return:
      Discovered subclasses in discovery (breadth-first) order. Each
      :class:`SubclassInfo` carries the ``depth`` at which it was found.

  .. deprecated::
      Prefer :func:`find_subclasses_local`, which determines its own roots from
      the caller's location and searches upward through the package ancestry
      instead of recursively downward through an explicit, fixed ``roots``.
      This function is kept for callers that genuinely want a fixed-root,
      recursive, downward scan (e.g. tooling that inspects an arbitrary,
      unrelated directory tree).
  """
  warnings.warn(
    "find_subclasses() is deprecated; prefer find_subclasses_local() for caller-relative searches.",
    DeprecationWarning,
    stacklevel=2,
  )
  index = _build_subclass_index(roots, ignored_dirs=ignored_dirs)
  return index.descendants(base, include_name_fallback=include_name_fallback, recursive=recursive)


def load_subclasses(
  base: type | str,
  roots: Iterable[StrPath] | StrPath,
  *,
  ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
  include_name_fallback: bool = True,
  recursive: bool | int = True,
) -> list[type]:
  """
  Find and import subclasses of ``base`` beneath ``roots``.

  This is the eager counterpart to :func:`find_subclasses`: each discovered
  module is imported and its class object returned, which also registers the
  subclass with :py:meth:`type.__subclasses__` for the rest of the process.

  :param base:
      The base class (a live class object or ``"module.Qual"`` string).
  :param roots:
      A single path or an iterable of paths to scan.
  :param ignored_dirs:
      Directory names to skip while walking.
  :param include_name_fallback:
      When ``True`` (default), also seed from subclasses whose base merely
      shares ``base``'s bare name. This catches subclasses whose imports could
      not be resolved statically, at the small risk of matching an unrelated
      class of the same name.
  :param recursive:
      Controls how deep the search descends. ``True`` (default) follows the
      inheritance chain to unlimited depth; ``False`` returns only immediate
      (depth ``1``) subclasses; an integer caps the search at that maximum depth.
      A limit of ``0`` or less yields nothing.

  :return:
      The imported subclass objects. Entries that fail to import are skipped and
      logged at ``WARNING`` level.

  .. deprecated::
      Prefer :func:`find_subclasses_local` followed by :meth:`SubclassInfo.load`
      on the results, for the same caller-relative reasons as
      :func:`find_subclasses`.
  """
  warnings.warn(
    "load_subclasses() is deprecated; prefer find_subclasses_local() + SubclassInfo.load().",
    DeprecationWarning,
    stacklevel=2,
  )
  index = _build_subclass_index(roots, ignored_dirs=ignored_dirs)
  loaded: list[type] = []
  for info in index.descendants(base, include_name_fallback=include_name_fallback, recursive=recursive):
    try:
      loaded.append(info.load())
    except (ImportError, AttributeError, TypeError) as exc:
      logger.warning("Could not load discovered subclass %s: %s", info.qualname, exc)
  return loaded


def find_subclasses_local(
  base: type | str,
  caller_file: str | None = None,
  ceiling_dir: str | None = None,
  *,
  include_name_fallback: bool = False,
  recursive: bool | int = True,
) -> tuple[SubclassInfo, ...]:
  """
  Statically find subclasses of ``base`` in the caller's own directory ancestry.

  Unlike :func:`find_subclasses`, this never takes an explicit set of roots to
  scan recursively downward. Instead it walks upward from ``caller_file``'s
  directory to ``ceiling_dir``, scanning only the direct (non-recursive) ``.py``
  files at each level -- sibling directories and subdirectories, including
  those under the caller's own directory, are never seen. No modules are
  imported; results are tagged with :attr:`SubclassInfo.locality` (ancestry
  depth from the caller) as well as the usual inheritance ``depth``.

  Results are sorted by locality first (closer to the caller wins), then by
  inheritance depth (deeper, more-derived wins) as a tiebreaker -- so
  ``find_subclasses_local(Base)[0]`` is always "the most locally-defined,
  most-derived" match, the natural choice for something like
  ``CapturesSubclasses.get_deepest_subclass``.

  Only files within the resolved ``[caller directory, ceiling_dir]`` ancestry
  window are ever parsed: the search is lazy and never indexes anything
  outside that window, though results for a given frozen set of files are
  cached (shared with :func:`find_subclasses`) so repeated calls with the same
  effective window reuse earlier work.

  :param base:
      The base class (a live class object or ``"module.Qual"`` string).
  :param caller_file:
      The file to treat as the search's starting point. Defaults to the direct
      caller of this function (via :func:`get_caller_file`); pass this
      explicitly when the natural call stack does not reflect the real
      starting point (e.g. a wrapper that itself wants to search from *its*
      caller's location rather than its own).
  :param ceiling_dir:
      The directory to stop climbing at. Defaults to
      ``get_package_root(caller_file)`` -- the top of the caller's own package.
  :param include_name_fallback:
      When ``True``, also seed from subclasses whose base merely shares
      ``base``'s bare name. This catches subclasses whose imports could not be
      resolved statically, at the small risk of matching an unrelated class of
      the same name.
  :param recursive:
      Controls how deep the search descends. ``True`` (default) follows the
      inheritance chain to unlimited depth; ``False`` returns only immediate
      (depth ``1``) subclasses; an integer caps the search at that maximum depth.
      A limit of ``0`` or less yields nothing.

  :return:
      Discovered subclasses sorted by ``(locality, -depth)``. Each
      :class:`SubclassInfo` carries both the inheritance ``depth`` at which it
      was found and its ancestry ``locality``.
  """
  if caller_file is None:
    caller_file = get_caller_file(1)
    if caller_file is None:
      raise RuntimeError("find_subclasses_local: could not automatically determine the calling file; pass caller_file explicitly.")
  if ceiling_dir is None:
    ceiling_dir = get_package_root(caller_file)

  locality_by_file = _collect_ancestry_files(caller_file, ceiling_dir)
  files = frozenset(locality_by_file)

  def build() -> _SubclassIndex:
    resolved: list[_ResolvedClass] = []
    for path in files:
      resolved.extend(_index_file(path))
    return _SubclassIndex(resolved)

  index = _INDEX_CACHE.get_or_compute(files, build)
  results = index.descendants(base, include_name_fallback=include_name_fallback, recursive=recursive)

  tagged = tuple(info._replace(locality=locality_by_file.get(info.file, 0)) for info in results)
  return tuple(sorted(tagged, key=lambda info: (info.locality, -info.depth)))
