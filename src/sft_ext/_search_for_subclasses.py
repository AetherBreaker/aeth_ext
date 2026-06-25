# This file was AI generated.

# Standard library imports
import ast
from importlib import import_module
from logging import getLogger
from os import PathLike, fspath, scandir
from os.path import abspath, basename, dirname, isdir, isfile, join, splitext
from sys import argv, modules
from typing import TYPE_CHECKING, NamedTuple

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
  "get_entrypoint_root",
  "iter_python_files",
  "load_subclasses",
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


def get_entrypoint_root() -> str:
  """
  Return the path of the top-most package containing the entrypoint script.

  Starting from the ``__main__`` module's file, this walks upward as long as each
  enclosing directory is a package (contains an ``__init__.py``) and returns the
  highest such package directory. When the entrypoint is a standalone script that
  is not part of any package, the directory holding it is returned.

  The result is the natural ``roots`` argument for :func:`find_subclasses` and
  friends: it is the widest directory guaranteed to share the entrypoint's import
  namespace.

  When the ``__main__`` module has no ``__file__`` -- as in a spawned
  :py:class:`~concurrent.futures.ProcessPoolExecutor`/:py:mod:`multiprocessing`
  worker whose ``__main__`` is the bootstrap module -- this falls back to
  ``sys.argv[0]``, which the parent process preserves as the original entrypoint
  script path. Running under the interactive interpreter (where neither is
  available) is not supported and will raise :py:class:`AttributeError`.

  :return:
      Absolute path of the top-most package directory, or the entrypoint's own
      directory when it is not packaged.
  """

  main_file = getattr(modules.get("__main__"), "__file__", None)

  if main_file is None:
    # In a spawned worker the bootstrap ``__main__`` has no ``__file__``, but the
    # parent's original ``sys.argv`` is restored in the child, so ``argv[0]``
    # still points at the real entrypoint script.
    entrypoint = abspath(argv[0]) if argv and argv[0] else None
    if entrypoint is None:
      raise AttributeError("module '__main__' has no attribute '__file__' and sys.argv[0] is unavailable")
    root = entrypoint if isdir(entrypoint) else dirname(entrypoint)
  else:
    root = dirname(abspath(main_file))

  while isfile(join(root, "__init__.py")):
    parent = dirname(root)
    if parent == root or not isfile(join(parent, "__init__.py")):
      break
    root = parent

  return root


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
  ) -> list[SubclassInfo]:
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
        Discovered subclasses ordered by qualified name, excluding ``base``
        itself. Each :class:`SubclassInfo` carries the ``depth`` at which it was
        found (``1`` for an immediate subclass). Under diamond inheritance a
        class is reported at its shallowest depth.
    """

    max_depth = self.__depth_limit(recursive)
    if max_depth is not None and max_depth <= 0:
      return []

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

    return sorted(result.values(), key=lambda item: item.qualname)

  def all_classes(self) -> list[SubclassInfo]:
    """Return every class discovered during the scan, ordered by qualified name."""

    return sorted(self.__by_qual.values(), key=lambda item: item.qualname)


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


# # ======================================================================================
# # TEMPORARY PROFILING HOOK -- remove this whole block (and the @__profile_subclass_scan
# # decorator on ``find_subclasses`` below) once a profile has been captured.
# #
# # Wraps the first ``find_subclasses`` call in this process with both a cProfile run
# # (CPU / call timings) and a tracemalloc snapshot (memory allocations), then writes
# # both artifacts next to the running entrypoint so they can be copied into the
# # ``sft_ext`` project root for analysis.
# # ======================================================================================
# def __profile_subclass_scan[**P, R](func: Callable[P, R]) -> Callable[P, R]:
#   # Standard library imports
#   import cProfile
#   import pstats
#   import tracemalloc
#   from datetime import datetime
#   from functools import wraps as __wraps
#   from pathlib import Path

#   @__wraps(func)
#   def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
#     out_dir = Path.cwd()
#     stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
#     cpu_path = join(out_dir, f"subclass_scan_{stamp}.prof")
#     mem_path = join(out_dir, f"subclass_scan_{stamp}.tracemalloc.txt")

#     tracemalloc.start(25)
#     profiler = cProfile.Profile()
#     profiler.enable()
#     try:
#       return func(*args, **kwargs)
#     finally:
#       profiler.disable()

#       profiler.dump_stats(cpu_path)
#       stats = pstats.Stats(profiler).sort_stats("cumulative")

#       snapshot = tracemalloc.take_snapshot()
#       tracemalloc.stop()
#       top_stats = snapshot.statistics("lineno")

#       with open(mem_path, "w", encoding="utf-8") as handle:
#         handle.write(f"# tracemalloc snapshot -- top allocations by line ({stamp})\n\n")
#         total = sum(stat.size for stat in top_stats)
#         handle.write(f"Total traced memory: {total / 1024:.1f} KiB across {len(top_stats)} lines\n\n")
#         for stat in top_stats[:50]:
#           handle.write(f"{stat}\n")
#         handle.write("\n# Top allocations with traceback\n\n")
#         for stat in snapshot.statistics("traceback")[:10]:
#           handle.write(f"{stat.count} blocks, {stat.size / 1024:.1f} KiB\n")
#           for line in stat.traceback.format():
#             handle.write(f"  {line}\n")
#           handle.write("\n")

#       logger.warning("Wrote CPU profile to %s", cpu_path)
#       logger.warning("Wrote memory profile to %s", mem_path)
#       stats.print_stats(40)

#   return wrapper


# @__profile_subclass_scan
def find_subclasses(
  base: type | str,
  roots: Iterable[StrPath] | StrPath,
  *,
  ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
  include_name_fallback: bool = True,
  recursive: bool | int = True,
) -> list[SubclassInfo]:
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
      Discovered subclasses ordered by qualified name. Each
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
