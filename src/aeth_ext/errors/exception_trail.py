"""Standardized fatal-exception-origin trail (replaces ``extract_details_callable``, TODO.md #6).

``build_exception_trail`` walks an exception's traceback (and, by default, its ``__cause__``/``__context__``
chain) into an ``ExceptionTrail``: an ordered, deduplicated, origin-first sequence of ``TrailEntry``
records, each categorized as first-party, third-party, stdlib, or unpackaged. ``ExceptionTrail.matches``
is the one standardized way to ask "did this failure touch marked module(s)" -- replacing every
consumer's own hand-rolled frame-walk/path-match logic.
"""

# Standard library imports
import os
import re
import sys
from dataclasses import dataclass
from enum import auto
from logging import getLogger
from os.path import abspath, isfile, join
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

# First party imports
from aeth_ext.static_eval import get_entrypoint_root, get_package_root
from aeth_ext.types import StrEnum

if TYPE_CHECKING:
  # Standard library imports
  from types import FrameType

logger = getLogger(__name__)

__all__ = ["ExceptionTrail", "OriginCategory", "TrailEntry", "build_exception_trail"]

# os is always a real stdlib module with a real backing file, so its own directory is the
# interpreter's actual stdlib location -- used to tell a genuine stdlib frame apart from a
# first/third-party module that merely shadows a stdlib top-level name (e.g. an application's own
# json.py).
_STDLIB_DIR = Path(abspath(os.__file__)).parent

# Directory names marking an installed-dependency root: "site-packages" everywhere, "dist-packages"
# on Debian/Ubuntu's system Python. On a non-venv POSIX install these commonly live *inside*
# _STDLIB_DIR (e.g. {prefix}/lib/python3.X/site-packages), so a stdlib-path check must exclude them
# explicitly -- otherwise an installed dependency that shadows a stdlib top-level name (its own
# json.py) would satisfy "is_relative_to(_STDLIB_DIR)" and get misfiled as STDLIB.
_INSTALLED_PACKAGE_DIR_NAMES = frozenset({"site-packages", "dist-packages"})


def _is_installed_package_root(root: str) -> bool:
  return not _INSTALLED_PACKAGE_DIR_NAMES.isdisjoint(root.replace("\\", "/").split("/"))


class OriginCategory(StrEnum):
  """Where a ``TrailEntry``'s module lives, relative to the running application."""

  FIRST_PARTY = auto()
  """Belongs to the application that is running: its package root matches ``get_entrypoint_root()``
  exactly, or is nested beneath it (a standalone entrypoint's own imported submodules)."""

  THIRD_PARTY = auto()
  """An installed dependency, or a resolvable package that is not the host application's own."""

  STDLIB = auto()
  """Part of the Python standard library (``sys.stdlib_module_names``)."""

  UNPACKAGED = auto()
  """Code with no real backing file (``exec``, a zip import), or a package root that is neither the
  entrypoint root nor nested beneath it and has no ``__init__.py`` of its own -- a standalone
  entrypoint's *own* root is ``FIRST_PARTY``, not this."""


class TrailEntry(NamedTuple):
  """One distinct module transition in an ``ExceptionTrail``, origin-first."""

  module: str
  category: OriginCategory
  file: str


_SEGMENT_STAR = "*"
_SEGMENT_DOUBLE_STAR = "**"


def _compile_pattern(pattern: str) -> re.Pattern[str]:
  """Compile a dot-segment glob *pattern* into a fully-anchored regex.

  ``*`` matches exactly one segment; ``**`` matches zero or more segments, including the separating dot on
  whichever side has a neighboring segment -- this is what lets ``"a.**.b"`` match the zero-segment case
  ``"a.b"``, not just ``"a.x.b"``. A ``**`` with a segment on only one side swallows only that side's dot; a
  bare ``"**"`` matches any non-empty dotted name. Not cached: patterns are typically static constants at
  a call site, so compiling per ``ExceptionTrail.matches()`` call costs nothing worth caching against.

  Consecutive ``**`` segments (e.g. ``"**.**"``) are collapsed into one before the main loop: each already
  means "zero or more segments", so two adjacent ones are equivalent to a single one, but generating a
  separate regex piece per occurrence produces two half-open groups (one missing its leading dot, one its
  trailing dot) that can't jointly match anything -- not the union their individual semantics promise.
  """
  raw_segments = pattern.split(".")
  segments: list[str] = []
  for seg in raw_segments:
    if seg == _SEGMENT_DOUBLE_STAR and segments and segments[-1] == _SEGMENT_DOUBLE_STAR:
      continue
    segments.append(seg)
  pieces: list[str] = []
  for i, seg in enumerate(segments):
    has_prev = i > 0
    has_next = i < len(segments) - 1
    if seg == _SEGMENT_DOUBLE_STAR:
      if has_prev and has_next:
        pieces.append(r"\.(?:[^.]+\.)*")
      elif has_next:
        pieces.append(r"(?:[^.]+\.)*")
      elif has_prev:
        pieces.append(r"(?:\.[^.]+)*")
      else:
        pieces.append(r".+")
      continue
    if has_prev and segments[i - 1] != _SEGMENT_DOUBLE_STAR:
      pieces.append(r"\.")
    pieces.append(r"[^.]+" if seg == _SEGMENT_STAR else re.escape(seg))
  return re.compile(f"^{''.join(pieces)}$")


def _categorize(module: str, file: str | None, entrypoint_root: str, *, is_frozen: bool = False) -> OriginCategory:
  """Categorize a single resolved ``(module, file)`` pair.

  Order matters: stdlib is checked first. A stdlib top-level name alone is trusted only when
  *is_frozen* is set (a genuine frozen frame, e.g. ``<frozen importlib._bootstrap>``, has no
  package root to check anyway -- a missing *file* alone is not enough evidence: dynamically
  compiled code, e.g. ``exec`` under a local module deliberately or coincidentally named ``json``,
  also has no real backing file but is not stdlib) or when *file* actually lives under the
  interpreter's own stdlib directory *and* not under an installed-package directory nested inside
  it -- name alone would otherwise misfile a first/third-party module that merely shadows a stdlib
  top-level name (e.g. an application's own ``json.py``) as ``STDLIB``; the installed-package
  exclusion additionally matters because a non-venv POSIX install commonly places
  ``site-packages``/``dist-packages`` *inside* the stdlib directory (e.g.
  ``{prefix}/lib/python3.X/site-packages``), where a plain ``is_relative_to`` check alone would
  still match it. Once stdlib is ruled out, root equality against *entrypoint_root* is checked before
  the installed-package third-party fallback: an installed host application (launched from within its
  own ``site-packages``/``dist-packages`` install) has its own frames' root land there too, matching
  *entrypoint_root* exactly -- checking the installed-package directory first would misfile the host
  application's own frames as ``THIRD_PARTY``. A missing *file* at that point has no package root to
  compute, so it falls straight to ``UNPACKAGED``.

  A standalone (non-packaged) entrypoint script is represented by its own containing directory,
  not a package root (``get_package_root()`` has nothing to climb from a directory with no
  ``__init__.py``) -- so a packaged module the entrypoint imports (e.g. ``/app/main.py`` importing
  ``/app/myapp/worker.py``) has a *different*, nested package root (``/app/myapp``) that never
  equals *entrypoint_root* (``/app``) by plain equality. Once the installed-package check has ruled
  out a project-local virtualenv nested under that same directory, any other root nested inside
  *entrypoint_root* is still the same standalone application's own code, so it is treated as
  ``FIRST_PARTY`` too, not just an exact root match.

  A PEP 420 namespace package (no ``__init__.py``) found directly under an installed-package
  directory is still ``THIRD_PARTY``, not ``UNPACKAGED`` -- the installed-package check runs before
  the ``__init__.py`` existence check specifically so a namespace dependency isn't misfiled just for
  lacking one.

  *entrypoint_root* is computed once by the caller (``_build_entries``), not here -- the entrypoint
  cannot change within a single ``build_exception_trail`` call, and ``get_entrypoint_root()`` walks
  the filesystem, so recomputing it per frame would cost real time for no different answer.
  """
  top_level = module.partition(".")[0]
  if top_level in sys.stdlib_module_names and (
    is_frozen
    or (file is not None and Path(abspath(file)).is_relative_to(_STDLIB_DIR) and not _is_installed_package_root(abspath(file)))
  ):
    return OriginCategory.STDLIB
  if file is None:
    return OriginCategory.UNPACKAGED
  return _categorize_by_root(get_package_root(file), entrypoint_root)


def _categorize_by_root(root: str, entrypoint_root: str) -> OriginCategory:
  """The non-stdlib half of ``_categorize``: classify an already-resolved package *root*.

  Split out from ``_categorize`` only because a lint rule (``PLR0911``, too many returns) forces
  it once the standalone-entrypoint nesting case was added -- not a judgment call. See
  ``_categorize``'s docstring for why the checks below are ordered the way they are.
  """
  if root == entrypoint_root:
    return OriginCategory.FIRST_PARTY
  if _is_installed_package_root(root):
    return OriginCategory.THIRD_PARTY
  if Path(root).is_relative_to(entrypoint_root):
    return OriginCategory.FIRST_PARTY
  if not isfile(join(root, "__init__.py")):
    return OriginCategory.UNPACKAGED
  return OriginCategory.THIRD_PARTY


def _resolve_frame(frame: FrameType) -> tuple[str, str | None, bool]:
  """Return ``(module, file, is_frozen)`` for *frame*. ``module`` falls back to ``"<unknown>"`` only
  when ``__name__`` itself is missing; ``file`` is ``None`` when the frame has no real, existing
  backing file (``exec``, a zip import, or a source file moved after compilation) -- kept separate
  from a missing module so a frame with a known name but synthetic file doesn't lose that name
  entirely. ``is_frozen`` is ``True`` only for CPython's own frozen-module marker (``<frozen ...>``,
  e.g. ``<frozen importlib._bootstrap>``) -- a missing *file* alone is not sufficient evidence of a
  genuinely frozen origin, since dynamically compiled code (``exec``) also has no real backing file
  but is not stdlib.
  """
  module = frame.f_globals.get("__name__") or "<unknown>"
  raw_file = frame.f_code.co_filename
  is_frozen = raw_file.startswith("<frozen ") and raw_file.endswith(">")
  if not raw_file or not isfile(raw_file):
    return module, None, is_frozen
  return module, raw_file, False


def _frames_innermost_first(exc: BaseException) -> list[FrameType]:
  """Every frame on ``exc.__traceback__``, innermost (where it was raised) first."""
  frames: list[FrameType] = []
  tb = exc.__traceback__
  while tb is not None:
    frames.append(tb.tb_frame)
    tb = tb.tb_next
  frames.reverse()
  return frames


def _reachable_via_ancestry(target: BaseException, start: BaseException) -> bool:
  """Whether *target* is reachable from *start* by following ``__cause__``/``__context__``, in any
  combination, any number of hops. ``id()``-based cycle guard, local to this walk.

  This is what tells a genuinely unrelated cause/context apart from one that merely differs by
  identity: e.g. ``except cause_exc: raise X from cause_exc`` while a *different* exception (``ctx``)
  is also active sets ``X.__context__ = ctx`` and ``X.__cause__ = cause_exc`` -- two different objects,
  but if ``ctx.__context__ is cause_exc`` (also common: ``cause_exc`` was itself active when ``ctx`` was
  raised), the two are still fully ordered ancestry, not two independent branches.
  """
  seen: set[int] = set()
  stack: list[BaseException] = [start]
  while stack:
    node = stack.pop()
    if id(node) in seen:
      continue
    seen.add(id(node))
    if node is target:
      return True
    if node.__cause__ is not None:
      stack.append(node.__cause__)
    if node.__context__ is not None:
      stack.append(node.__context__)
  return False


def _safe_repr(obj: object) -> str:
  """``repr(obj)``, or a type-name fallback if ``repr`` itself raises -- diagnostic message
  formatting must never be the reason ``build_exception_trail`` fails for a direct caller (see
  ``_warn_ambiguous_ancestry``): a custom exception with a broken ``__repr__`` must not defeat the
  best-effort guarantee just by being interpolated into the message before either guard runs.
  """
  try:
    return repr(obj)
  except Exception:  # noqa: BLE001 -- best-effort formatting, see docstring
    return f"<{type(obj).__name__} instance (repr failed)>"


def _warn_ambiguous_ancestry(node: BaseException, cause: BaseException, context: BaseException) -> None:
  """Flag a node whose explicit ``__cause__`` and implicit ``__context__`` are two different exceptions
  with no ancestry relationship between them at all (see ``_reachable_via_ancestry``): ``raise X from Y``
  fired while implicitly propagating out of a wholly separate failure Z, where neither Y nor Z is an
  ancestor of the other. Nothing in the objects says which of Y/Z happened first -- ``_expand_oldest_first``
  breaks the tie by walking cause before context, but this shape is confusing enough on its own
  that it should never survive in production code, so it's surfaced loudly rather than silently
  ordered: CRITICAL on the logger and a direct stderr line, so it can't be missed or filtered out.

  Both emissions -- and the message formatting itself, via ``_safe_repr`` -- are best-effort: a
  broken diagnostic sink (closed stderr, a misbehaving logging filter/handler) or a broken
  ``__repr__`` on *node*/*cause*/*context* must not fail trail construction for a direct
  ``build_exception_trail`` caller -- its documented failure condition is only a missing traceback,
  and ``ambiguous_ancestry`` is already recorded correctly by the caller regardless of whether this
  notification itself lands.
  """
  msg = (
    f"Ambiguous exception ancestry: {_safe_repr(node)} has an explicit cause ({_safe_repr(cause)}) that "
    f"differs from its implicit context ({_safe_repr(context)}) -- a 'raise ... from' fired while "
    "propagating out of an unrelated failure. This pattern should be removed from the code that produced it."
  )
  try:
    logger.critical(msg)
  except Exception:  # noqa: BLE001, S110 -- best-effort notification, see docstring
    pass
  try:
    print(msg, file=sys.stderr)
  except Exception:  # noqa: BLE001, S110 -- best-effort notification, see docstring
    pass


def _expand_oldest_first(exc: BaseException, *, walk_chain: bool, walk_groups: bool) -> tuple[list[BaseException], bool]:
  """*exc*, optionally its ``__cause__``/``__context__`` ancestors and/or its
  ``BaseExceptionGroup`` members, oldest/deepest first, ``exc`` itself last.

  When *walk_chain* is ``True``, both cause and context links are followed at every node, not just
  whichever is set -- ``raise X from Y`` inside an active ``except Z:`` block sets ``__cause__`` to
  Y (explicit) and ``__context__`` to Z (implicit) independently, and either can carry real
  ancestry the other doesn't. When *walk_groups* is ``True``, a ``BaseExceptionGroup``'s
  ``.exceptions`` are walked too (recursively, so a nested group's own members are also expanded) --
  a group's members are what it *consists of*, a compositional relationship independent of the
  temporal cause/context chain, hence the separate flag: a caller can walk one axis without the
  other. Member order matches ``exc.exceptions`` (index 0 first).

  Iterative (an explicit stack, not recursive calls) -- the standard iterative-postorder trick:
  each stack entry is ``(node, expanded)``, ``False`` meaning "requeue as expanded, then push its
  children" and ``True`` meaning "children are already in ``ordered``, append ``node`` now".
  Children are pushed context-then-cause-then-members(reversed) so members pop first (fully
  resolving each in ``exc.exceptions`` order before the next), then cause, then context -- this
  also sidesteps Python's recursion limit on a pathologically deep chain or wide group.

  ``id()``-based ``seen`` set does double duty across both edge kinds: stops a ``__context__`` cycle
  (or a degenerate group membership cycle) from hanging the walk, and dedupes a node reachable via
  more than one edge (e.g. ``raise X from e`` while already handling ``e``, where cause and context
  are the same object -- the ordinary, unambiguous case).

  Returns the ordered exception list, plus whether any node had both cause and context set to
  different exceptions with no ancestry relationship between them at all -- see
  ``_reachable_via_ancestry``/``_warn_ambiguous_ancestry``. Always ``False`` when *walk_chain* is
  ``False``, since that check only applies to the cause/context axis.
  """
  seen: set[int] = set()
  ordered: list[BaseException] = []
  ambiguous = False
  stack: list[tuple[BaseException, bool]] = [(exc, False)]
  while stack:
    node, expanded = stack.pop()
    if expanded:
      ordered.append(node)
      continue
    if id(node) in seen:
      continue
    seen.add(id(node))
    cause, context = (node.__cause__, node.__context__) if walk_chain else (None, None)
    if (
      cause is not None
      and context is not None
      and cause is not context
      and not _reachable_via_ancestry(cause, context)
      and not _reachable_via_ancestry(context, cause)
    ):
      ambiguous = True
      _warn_ambiguous_ancestry(node, cause, context)
    stack.append((node, True))
    if context is not None:
      stack.append((context, False))
    if cause is not None:
      stack.append((cause, False))
    if walk_groups and isinstance(node, BaseExceptionGroup):
      stack.extend((member, False) for member in reversed(node.exceptions))
  return ordered, ambiguous


def _build_entries(exc: BaseException, *, walk_chain: bool, walk_groups: bool) -> tuple[tuple[TrailEntry, ...], bool]:
  """Walk *exc* (and optionally its full cause/context ancestry and/or group members) into
  deduplicated, origin-first ``TrailEntry`` tuples, plus whether ambiguous ancestry was found
  (see ``_expand_oldest_first``).
  """
  exceptions, ambiguous_ancestry = _expand_oldest_first(exc, walk_chain=walk_chain, walk_groups=walk_groups)
  # get_entrypoint_root() stops at a runnable subpackage's own __main__.py boundary (e.g.
  # aeth_ext.central_log_server), while get_package_root() for its frames climbs to the
  # top-level aeth_ext package -- normalize through get_package_root() here so the FIRST_PARTY
  # comparison in _categorize() compares like-for-like package roots.
  entrypoint_root = get_package_root(join(get_entrypoint_root(), "__init__.py"))

  entries: list[TrailEntry] = []
  last_module: str | None = None
  for one_exc in exceptions:
    for frame in _frames_innermost_first(one_exc):
      module, file, is_frozen = _resolve_frame(frame)
      if module == last_module:
        continue
      category = _categorize(module, file, entrypoint_root, is_frozen=is_frozen)
      entries.append(TrailEntry(module=module, category=category, file=file or "<unknown>"))
      last_module = module

  return tuple(entries), ambiguous_ancestry


@dataclass(frozen=True, slots=True)
class ExceptionTrail:
  """A single fatal exception's full origin trail, origin-first, with consecutive same-module frames
  collapsed into one entry -- a module revisited later in the trail (e.g. A -> B -> A) still appears
  more than once, so ``entries`` is not globally unique by module.

  Built once by ``build_exception_trail`` and never mutated -- every attribute below is computed
  eagerly at construction, not lazily, since the walk itself already dominates the cost.
  """

  entries: tuple[TrailEntry, ...]
  origin: TrailEntry
  first_party_entry: TrailEntry | None
  ambiguous_ancestry: bool
  """``True`` if walking ``__cause__``/``__context__`` found a node where both are set to different,
  unrelated exceptions -- see ``_expand_oldest_first``/``_warn_ambiguous_ancestry``. Always ``False``
  when ``walk_chain=False``."""

  def matches(self, *patterns: str) -> tuple[TrailEntry, ...]:
    """Every entry whose module matches any of *patterns* (see ``_compile_pattern`` for glob syntax).

    Returns entries in trail order (origin-first). The empty tuple is falsy, so
    ``if trail.matches(...):`` reads as a boolean check while still handing back full match detail to a
    caller that wants it.
    """
    compiled = [_compile_pattern(p) for p in patterns]
    return tuple(entry for entry in self.entries if any(p.fullmatch(entry.module) for p in compiled))


def build_exception_trail(exc: BaseException, *, walk_chain: bool = True, walk_groups: bool = True) -> ExceptionTrail:
  """Build the full origin trail for *exc*.

  Args:
    exc: The exception to walk. Must have a live ``__traceback__`` (i.e. called from inside the
      ``except`` block that caught it, or with a manually-attached traceback).
    walk_chain: When ``True`` (default), also walks ``__cause__``/``__context__`` recursively, both
      links at every node (not just whichever is set), with ``id()``-based cycle detection. Matches
      the app-side ``_is_database_origin_exception`` behavior this trail replaces. See
      ``ExceptionTrail.ambiguous_ancestry`` for the one case this can't order from the objects alone.
    walk_groups: When ``True`` (default), also walks a ``BaseExceptionGroup``'s ``.exceptions``
      recursively (a nested group's own members are expanded the same way), in ``exc.exceptions``
      order, each member's frames appearing before the group's own container frames. A caller that
      only cares about where the group itself was raised (e.g. a ``TaskGroup``'s own aggregation
      point), not the individual failures it wraps, can pass ``False`` to skip this -- independent
      of *walk_chain*, since group membership is compositional, not a temporal cause/context chain.

  Returns:
    An ``ExceptionTrail`` whose ``entries`` is never empty -- an exception always has at least one
    frame, the one that raised it.

  Raises:
    ValueError: *exc* has no live ``__traceback__`` (e.g. it was constructed but never raised).
  """
  if exc.__traceback__ is None:
    raise ValueError(f"build_exception_trail requires a live traceback -- {_safe_repr(exc)} was never raised")
  entries, ambiguous_ancestry = _build_entries(exc, walk_chain=walk_chain, walk_groups=walk_groups)
  first_party = next((e for e in entries if e.category is OriginCategory.FIRST_PARTY), None)
  return ExceptionTrail(
    entries=entries, origin=entries[0], first_party_entry=first_party, ambiguous_ancestry=ambiguous_ancestry
  )
