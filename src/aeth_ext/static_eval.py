# This file was mostly AI generated.

# Standard library imports
import ast
import inspect
from collections.abc import Iterator
from functools import wraps
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
  "SubclassIndex",
  "SubclassInfo",
  "build_subclass_index",
  "find_subclasses",
  "get_cls_scan_root",
  "get_entrypoint_root",
  "iter_python_files",
  "load_subclasses",
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
  """

  qualname: str
  name: str
  module: str
  file: str
  lineno: int
  depth: int = 0

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

  for fp in fps:
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


def __format_call_stack() -> str:
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


def get_entrypoint_root(main_file: str | None = None) -> str:
  """
  Return the path of the top-most package containing the entrypoint script.

  Starting from the ``__main__`` module's file, this walks upward as long as each
  enclosing directory is a package (contains an ``__init__.py``) and returns the
  highest such package directory. When the entrypoint is a standalone script that
  is not part of any package, the directory holding it is returned.

  The walk stops early if a directory contains a ``__main__.py``: that marks it
  as a directly-runnable package, making it the natural boundary rather than a
  mere namespace component that happens to be part of a larger package.

  The result is the natural ``roots`` argument for :func:`find_subclasses` and
  friends: it is the widest directory guaranteed to share the entrypoint's import
  namespace.

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

  if main_file is None:
    main_module = modules.get("__main__")
    spec = getattr(main_module, "__spec__", None)

    # Strategy 1: __spec__.origin — set when Python runs with -m.
    spec_origin = getattr(spec, "origin", None)
    if spec_origin and isfile(spec_origin):
      root = dirname(abspath(spec_origin))
    else:
      # Strategy 2: resolve the package via importlib from __spec__'s name.
      # Handles launchers that set __spec__ but leave origin=None.
      spec_name = getattr(spec, "parent", None) or getattr(spec, "name", None)
      root = None
      if spec_name:
        # Standard library imports
        from importlib.util import find_spec as _find_spec

        try:
          found = _find_spec(spec_name)
          if found and found.origin and isfile(found.origin):
            root = dirname(abspath(found.origin))
        except Exception:
          pass

      if root is None:
        # Strategy 3: argv[0] — reliable for spawned multiprocessing workers
        # where the parent's sys.argv is preserved in the child.
        entrypoint = abspath(argv[0]) if argv and argv[0] else None
        if entrypoint is None:
          raise AttributeError("module '__main__' has no attribute '__file__' and sys.argv[0] is unavailable")
        root = entrypoint if isdir(entrypoint) else dirname(entrypoint)
  else:
    root = dirname(abspath(main_file))

  while isfile(join(root, "__init__.py")):
    main_py_path = join(root, "__main__.py")
    if isfile(main_py_path):
      # Check if this __main__.py has SKIP_ENTRYPOINT_MARKER set to True
      skip_marker_result = __parse_and_grab_constants(
        Path(main_py_path),
        expected_constants={"skip_entrypoint_marker": "skip_marker"},
      )
      # Only treat as a package boundary if SKIP_ENTRYPOINT_MARKER is not True
      if not skip_marker_result.get("skip_marker", False):
        break
    parent = dirname(root)
    if parent == root or not isfile(join(parent, "__init__.py")):
      break
    root = parent

  return root


def _get_default_search_paths() -> tuple[Path, ...]:
  """
  Compute the default search paths for :func:`parse_and_grab_constants`.

  Called at use time (never cached) so that :data:`sys.modules`[``"__main__"``]
  is read after Python has fully populated it.  Importing this module as a
  parent-package side-effect (e.g. via :mod:`runpy` during ``python -m``)
  would otherwise capture the bootstrap ``__main__``, not the real one.

  When :func:`get_entrypoint_root` returns a directory with no ``__init__.py``
  (meaning the bootstrap ``__main__`` is still active), this falls back to
  deriving the package root from *this module's own* ``__file__``.  That
  anchor is always valid — ``static_eval.py`` is inside the top-level
  ``aeth_ext`` package, whether running from source or from site-packages.
  """
  first_root = Path(get_entrypoint_root())

  # If get_entrypoint_root returned a path with no __init__.py it resolved to a
  # non-package directory (e.g. /app when __main__ is still the runpy bootstrap).
  # Walk up from __file__ instead — it is always inside the real package tree.
  if not isfile(str(first_root / "__init__.py")):
    pkg_root = Path(__file__).parent
    while isfile(str(pkg_root.parent / "__init__.py")):
      pkg_root = pkg_root.parent
    first_root = pkg_root

  _argv0 = argv[0] if argv and argv[0] and (isfile(abspath(argv[0])) or isdir(abspath(argv[0]))) else None
  second_root = Path(get_entrypoint_root(_argv0)) if _argv0 else first_root
  if not isfile(str(second_root / "__init__.py")):
    second_root = first_root

  main_module = modules.get("__main__")
  _ep_file: str | None = getattr(main_module, "__file__", None)
  if _ep_file is None:
    _spec_origin = getattr(getattr(main_module, "__spec__", None), "origin", None)
    _ep_file = _spec_origin or _argv0 or __file__

  result = (
    first_root / "__init__.py",
    second_root / "__init__.py",
    first_root / "__main__.py",
    second_root / "__main__.py",
    Path(_ep_file),  # pyright: ignore[reportArgumentType]
  )
  return result


def __getattr__(name: str) -> Any:
  """Lazy module attributes — ``DEFAULT_SEARCH_PATHS`` is deferred to call time."""
  if name == "DEFAULT_SEARCH_PATHS":
    return _get_default_search_paths()
  raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_cls_scan_root(cls: type) -> str:
  """
  Return the appropriate filesystem root to scan when looking for subclasses
  of ``cls``.

  When ``cls`` lives inside a ``site-packages`` directory (i.e. it is part of
  an installed package), the scan is scoped to that class's own top-level
  package directory (e.g. ``…/site-packages/aeth_ext``).  This prevents the
  scanner from walking unrelated installed packages at the same level.

  When ``cls`` lives outside ``site-packages`` (a normal development checkout),
  the usual :func:`get_entrypoint_root` value is returned.
  """
  # Standard library imports
  import sys as _sys

  module = _sys.modules.get(cls.__module__)
  cls_file = getattr(module, "__file__", None) or ""
  cls_path = Path(cls_file)

  if "site-packages" not in cls_path.parts:
    return get_entrypoint_root()

  # Locate the site-packages directory and scope to this class's top-level package
  # so that unrelated installed packages are never traversed.
  parts = cls_path.parts
  sp_idx = next(i for i, p in enumerate(parts) if p == "site-packages")
  site_packages_dir = Path(*parts[: sp_idx + 1])
  top_level_pkg = cls.__module__.split(".")[0]
  return str(site_packages_dir / top_level_pkg)


@wraps(__parse_and_grab_constants)
def parse_and_grab_constants(
  *fps: Path,
  expected_constants: dict[str, str],
  eval_locals: dict[str, Any] | None = None,
) -> dict[str, Any]:
  if not fps:
    fps = _get_default_search_paths()
  return __parse_and_grab_constants(*fps, expected_constants=expected_constants, eval_locals=eval_locals)


class __RawClass(NamedTuple):
  """A class definition captured before its bases have been resolved."""

  qualname: str
  name: str
  module: str
  file: str
  lineno: int
  bases: tuple[str, ...]


class __FileRecord(NamedTuple):
  """Everything extracted from a single source file, cached by ``mtime``/size."""

  module: str
  imports: dict[str, str]
  classes: tuple[__RawClass, ...]
  top_level: frozenset[str]


# A class paired with its resolved base edges: (raw class, base qualnames, base bare names).
type __ResolvedClass = tuple[__RawClass, frozenset[str], frozenset[str]]


# --- Process-wide caches -------------------------------------------------------
# Scanned files are assumed immutable for the lifetime of the process (no edits,
# no new files), so every cache below is keyed purely by content identity and is
# never invalidated. Work is therefore shared across calls and across overlapping
# ``roots``: each directory is walked once, each file is read/parsed/indexed once,
# and identical file sets reuse a fully prebuilt index. Call
# :func:`reset_subclass_caches` if that immutability assumption is ever broken.


class __OnceCache[K, V]:
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
__WALK_CACHE = __OnceCache[tuple[str, frozenset[str]], tuple[str, ...]]()

# Directory path -> whether it is a package (memoises ``__init__.py`` probing).
__PKG_CACHE = __OnceCache[str, bool]()

# File path -> parsed record (``None`` marks an unreadable/unparseable file).
__RECORD_CACHE = __OnceCache[str, __FileRecord | None]()

# File path -> that file's classes paired with their resolved base edges.
__EDGES_CACHE = __OnceCache[str, tuple[__ResolvedClass, ...]]()

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

  __WALK_CACHE.clear()
  __PKG_CACHE.clear()
  __RECORD_CACHE.clear()
  __EDGES_CACHE.clear()
  __INDEX_CACHE.clear()


def __normalize_roots(roots: Iterable[StrPath] | StrPath) -> list[str]:
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


def __walk_root(root: str, ignored_dirs: frozenset[str]) -> tuple[str, ...]:
  """Return the cached ``.py`` file listing for a single ``root`` directory."""

  key = (root, ignored_dirs)
  return __WALK_CACHE.get_or_compute(key, lambda: __scandir_walk(root, ignored_dirs))


def iter_python_files(
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
  for root in __normalize_roots(roots):
    for path in __walk_root(root, ignored_dirs):
      if path not in seen:
        seen.add(path)
        yield path


def __is_package(directory: str) -> bool:
  """Return whether ``directory`` is a package, memoised exactly once."""

  return __PKG_CACHE.get_or_compute(directory, lambda: isfile(join(directory, "__init__.py")))


def __module_qualname(path: str) -> str:
  """
  Derive the dotted module name for ``path`` from its package layout.

  Walks upward while each parent directory is a package (contains an
  ``__init__.py``); the package test is memoised across the whole scan.
  """

  name = splitext(basename(path))[0]
  parts = [] if name == "__init__" else [name]

  directory = dirname(path)
  while __is_package(directory):
    parts.append(basename(directory))
    directory = dirname(directory)

  parts.reverse()
  return ".".join(parts)


def __dotted_name(node: ast.expr) -> str | None:
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
      prefix = __dotted_name(value)
      return f"{prefix}.{attr}" if prefix is not None else None
    case ast.Subscript(value=value):
      return __dotted_name(value)
    case _:
      return None


def __resolve_from(node: ast.ImportFrom, package: str) -> str:
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


def __iter_nested_blocks(node: ast.stmt) -> Iterator[list[ast.stmt]]:
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


def __record_import(node: ast.Import, table: dict[str, str]) -> None:
  """Record bindings introduced by a plain ``import a.b.c [as x]`` statement."""

  for alias in node.names:
    if alias.asname:
      table[alias.asname] = alias.name
    else:
      # ``import a.b.c`` binds the top name ``a`` in the namespace.
      top = alias.name.partition(".")[0]
      table[top] = top


def __record_import_from(node: ast.ImportFrom, package: str, table: dict[str, str]) -> None:
  """Record bindings introduced by a ``from ... import ...`` statement."""

  target = __resolve_from(node, package)
  for alias in node.names:
    if alias.name == "*":
      continue
    local = alias.asname or alias.name
    table[local] = f"{target}.{alias.name}" if target else alias.name


def __collect_imports(body: list[ast.stmt], package: str, table: dict[str, str]) -> None:
  """
  Populate ``table`` mapping each locally bound name to its absolute target.

  Descends into ``if``/``try`` blocks so conditional imports are still resolved.
  """

  for node in body:
    if isinstance(node, ast.Import):
      __record_import(node, table)
    elif isinstance(node, ast.ImportFrom):
      __record_import_from(node, package, table)
    else:
      for block in __iter_nested_blocks(node):
        __collect_imports(block, package, table)


def __collect_classes(body: list[ast.stmt], enclosing: str, module: str, file: str, out: list[__RawClass]) -> None:
  """
  Append a :class:`__RawClass` for every class defined in ``body``.

  Descends into nested classes and ``if``/``try`` blocks but deliberately skips
  function bodies: classes defined inside functions cannot be reached by import
  alone, and skipping those subtrees keeps the walk tight.
  """

  for node in body:
    if isinstance(node, ast.ClassDef):
      qualname = f"{enclosing}.{node.name}"
      base_names = tuple(dotted for base in node.bases if (dotted := __dotted_name(base)) is not None)
      out.append(__RawClass(qualname, node.name, module, file, node.lineno, base_names))
      __collect_classes(node.body, qualname, module, file, out)
    else:
      for block in __iter_nested_blocks(node):
        __collect_classes(block, enclosing, module, file, out)


def __do_parse_file(path: str) -> __FileRecord | None:
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

  module = __module_qualname(path)
  package = module if basename(path) == "__init__.py" else module.rpartition(".")[0]

  imports: dict[str, str] = {}
  __collect_imports(tree.body, package, imports)

  classes: list[__RawClass] = []
  __collect_classes(tree.body, module, module, path, classes)

  top_level = frozenset(cls.name for cls in classes if cls.qualname == f"{module}.{cls.name}")
  return __FileRecord(module, imports, tuple(classes), top_level)


def __parse_file(path: str) -> __FileRecord | None:
  """
  Parse ``path`` into a :class:`__FileRecord`, memoised for the whole process.

  Files are assumed never to change, so a path is read and parsed exactly once
  (concurrent callers wait on the one in-flight parse); the result, including
  ``None`` for an unreadable or unparseable file, is cached. A single bad file
  never aborts a whole-tree scan.
  """

  return __RECORD_CACHE.get_or_compute(path, lambda: __do_parse_file(path))


def __compute_edges(path: str) -> tuple[__ResolvedClass, ...]:
  """Resolve ``path``'s classes to their base edges (uncached)."""

  record = __parse_file(path)
  if record is None:
    return ()

  return tuple(
    (
      cls,
      frozenset(__resolve_base(base, record.imports, record.top_level, record.module) for base in cls.bases),
      frozenset(base.rpartition(".")[2] for base in cls.bases),
    )
    for cls in record.classes
  )


def __index_file(path: str) -> tuple[__ResolvedClass, ...]:
  """
  Return ``path``'s classes paired with their resolved base edges, memoised.

  Base resolution depends only on the file's own imports and contents, so the
  result is stable per file and computed exactly once. This is the per-file
  "index" step shared across every :func:`build_subclass_index` call, including
  overlapping ``roots``.
  """

  return __EDGES_CACHE.get_or_compute(path, lambda: __compute_edges(path))


def __resolve_base(dotted: str, imports: dict[str, str], top_level: frozenset[str], module: str) -> str:
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


class SubclassIndex:
  """
  A reusable, statically built inheritance index.

  Build the index once with :func:`build_subclass_index` (or
  :func:`find_subclasses`) and query it for as many base classes as you like;
  the expensive parse happens a single time.
  """

  __slots__ = ("__by_qual", "__children_by_name", "__children_by_qual")

  def __init__(self, resolved: Iterable[__ResolvedClass]) -> None:
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
__INDEX_CACHE = __OnceCache[frozenset[str], SubclassIndex]()


def build_subclass_index(
  roots: Iterable[StrPath] | StrPath,
  *,
  ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
) -> SubclassIndex:
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

  files = frozenset(iter_python_files(roots, ignored_dirs=ignored_dirs))

  def build() -> SubclassIndex:
    resolved: list[__ResolvedClass] = []
    for path in files:
      resolved.extend(__index_file(path))
    return SubclassIndex(resolved)

  return __INDEX_CACHE.get_or_compute(files, build)


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
  """

  index = build_subclass_index(roots, ignored_dirs=ignored_dirs)
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
  """

  loaded: list[type] = []
  for info in find_subclasses(
    base, roots, ignored_dirs=ignored_dirs, include_name_fallback=include_name_fallback, recursive=recursive
  ):
    try:
      loaded.append(info.load())
    except (ImportError, AttributeError, TypeError) as exc:
      logger.warning("Could not load discovered subclass %s: %s", info.qualname, exc)
  return loaded
