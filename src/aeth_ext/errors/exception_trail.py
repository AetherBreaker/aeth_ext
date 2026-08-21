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


class OriginCategory(StrEnum):
  """Where a ``TrailEntry``'s module lives, relative to the running application."""

  FIRST_PARTY = auto()
  """Belongs to the application that is running (its package root matches ``get_entrypoint_root()``)."""

  THIRD_PARTY = auto()
  """An installed dependency, or a resolvable package that is not the host application's own."""

  STDLIB = auto()
  """Part of the Python standard library (``sys.stdlib_module_names``)."""

  UNPACKAGED = auto()
  """No resolvable package root -- a loose script, ``__main__``, or code with no real backing file."""


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


def _categorize(module: str, file: str | None, entrypoint_root: str) -> OriginCategory:
  """Categorize a single resolved ``(module, file)`` pair.

  Order matters: stdlib is checked first. A stdlib top-level name alone is trusted only when
  *file* is missing (a frozen frame, e.g. ``<frozen importlib._bootstrap>``, has no package root
  to check anyway) or when *file* actually lives under the interpreter's own stdlib directory --
  name alone would otherwise misfile a first/third-party module that merely shadows a stdlib
  top-level name (e.g. an application's own ``json.py``) as ``STDLIB``. Once stdlib is ruled out,
  root equality against *entrypoint_root* is checked before the ``site-packages`` third-party
  fallback: an installed host application (launched from within its own ``site-packages`` install)
  has its own frames' root land under ``site-packages`` too, matching *entrypoint_root* exactly --
  checking ``site-packages`` first would misfile the host application's own frames as
  ``THIRD_PARTY``. A missing *file* at that point has no package root to compute, so it falls
  straight to ``UNPACKAGED``.

  *entrypoint_root* is computed once by the caller (``_build_entries``), not here -- the entrypoint
  cannot change within a single ``build_exception_trail`` call, and ``get_entrypoint_root()`` walks
  the filesystem, so recomputing it per frame would cost real time for no different answer.
  """
  top_level = module.partition(".")[0]
  if top_level in sys.stdlib_module_names and (file is None or Path(abspath(file)).is_relative_to(_STDLIB_DIR)):
    return OriginCategory.STDLIB
  if file is None:
    return OriginCategory.UNPACKAGED

  root = get_package_root(file)
  if root == entrypoint_root:
    return OriginCategory.FIRST_PARTY
  if "site-packages" in root.replace("\\", "/").split("/"):
    return OriginCategory.THIRD_PARTY
  if not isfile(join(root, "__init__.py")):
    return OriginCategory.UNPACKAGED
  return OriginCategory.THIRD_PARTY


def _resolve_frame(frame: FrameType) -> tuple[str, str | None]:
  """Return ``(module, file)`` for *frame*. ``module`` falls back to ``"<unknown>"`` only when
  ``__name__`` itself is missing; ``file`` is ``None`` when the frame has no real, existing backing
  file (``exec``, a zip import, or a source file moved after compilation) -- kept separate from a
  missing module so a frame with a known name but synthetic file doesn't lose that name entirely."""
  module = frame.f_globals.get("__name__") or "<unknown>"
  file = frame.f_code.co_filename
  if not file or not isfile(file):
    return module, None
  return module, file


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


def _warn_ambiguous_ancestry(node: BaseException, cause: BaseException, context: BaseException) -> None:
  """Flag a node whose explicit ``__cause__`` and implicit ``__context__`` are two different exceptions
  with no ancestry relationship between them at all (see ``_reachable_via_ancestry``): ``raise X from Y``
  fired while implicitly propagating out of a wholly separate failure Z, where neither Y nor Z is an
  ancestor of the other. Nothing in the objects says which of Y/Z happened first -- ``_chain_oldest_first``
  breaks the tie by walking cause before context, but this shape is confusing enough on its own
  that it should never survive in production code, so it's surfaced loudly rather than silently
  ordered: CRITICAL on the logger and a direct stderr line, so it can't be missed or filtered out.
  """
  msg = (
    f"Ambiguous exception ancestry: {node!r} has an explicit cause ({cause!r}) that differs from "
    f"its implicit context ({context!r}) -- a 'raise ... from' fired while propagating out of an "
    "unrelated failure. This pattern should be removed from the code that produced it."
  )
  logger.critical(msg)
  print(msg, file=sys.stderr)


def _chain_oldest_first(exc: BaseException) -> tuple[list[BaseException], bool]:
  """*exc* and its ``__cause__``/``__context__`` ancestors, oldest first, ``exc`` last.

  Recursively walks BOTH links, not just whichever is set -- ``raise X from Y`` inside an active
  ``except Z:`` block sets ``__cause__`` to Y (explicit) and ``__context__`` to Z (implicit)
  independently, and either can carry real ancestry the other doesn't.

  Iterative (an explicit stack, not recursive calls) -- the standard two-child iterative-postorder
  trick: each stack entry is ``(node, expanded)``, ``False`` meaning "requeue as expanded, then push
  its children" and ``True`` meaning "children are already in ``ordered``, append ``node`` now".
  Children are pushed context-then-cause so cause pops (and so fully resolves, oldest-first) before
  context. This also sidesteps Python's recursion limit on a pathologically deep chain.

  ``id()``-based ``seen`` set does double duty: stops a ``__context__`` cycle from hanging the walk, and
  dedupes a node reachable via both links (e.g. ``raise X from e`` while already handling ``e``, where
  cause and context are the same object -- the ordinary, unambiguous case).

  Returns the ordered exception list, plus whether any node had both cause and context set to
  different exceptions with no ancestry relationship between them at all -- see
  ``_reachable_via_ancestry``/``_warn_ambiguous_ancestry``.
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
    cause, context = node.__cause__, node.__context__
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
  return ordered, ambiguous


def _build_entries(exc: BaseException, *, walk_chain: bool) -> tuple[tuple[TrailEntry, ...], bool]:
  """Walk *exc* (and optionally its full cause/context ancestry) into deduplicated, origin-first
  ``TrailEntry`` tuples, plus whether ambiguous ancestry was found (see ``_chain_oldest_first``)."""
  if walk_chain:
    exceptions, ambiguous_ancestry = _chain_oldest_first(exc)
  else:
    exceptions, ambiguous_ancestry = [exc], False
  # get_entrypoint_root() stops at a runnable subpackage's own __main__.py boundary (e.g.
  # aeth_ext.central_log_server), while get_package_root() for its frames climbs to the
  # top-level aeth_ext package -- normalize through get_package_root() here so the FIRST_PARTY
  # comparison in _categorize() compares like-for-like package roots.
  entrypoint_root = get_package_root(join(get_entrypoint_root(), "__init__.py"))

  entries: list[TrailEntry] = []
  last_module: str | None = None
  for one_exc in exceptions:
    for frame in _frames_innermost_first(one_exc):
      module, file = _resolve_frame(frame)
      if module == last_module:
        continue
      category = _categorize(module, file, entrypoint_root)
      entries.append(TrailEntry(module=module, category=category, file=file or "<unknown>"))
      last_module = module

  return tuple(entries), ambiguous_ancestry


@dataclass(frozen=True, slots=True)
class ExceptionTrail:
  """A single fatal exception's full origin trail, origin-first and deduplicated.

  Built once by ``build_exception_trail`` and never mutated -- every attribute below is computed
  eagerly at construction, not lazily, since the walk itself already dominates the cost.
  """

  entries: tuple[TrailEntry, ...]
  origin: TrailEntry
  first_party_entry: TrailEntry | None
  ambiguous_ancestry: bool
  """``True`` if walking ``__cause__``/``__context__`` found a node where both are set to different,
  unrelated exceptions -- see ``_chain_oldest_first``/``_warn_ambiguous_ancestry``. Always ``False``
  when ``walk_chain=False``."""

  def matches(self, *patterns: str) -> tuple[TrailEntry, ...]:
    """Every entry whose module matches any of *patterns* (see ``_compile_pattern`` for glob syntax).

    Returns entries in trail order (origin-first). The empty tuple is falsy, so
    ``if trail.matches(...):`` reads as a boolean check while still handing back full match detail to a
    caller that wants it.
    """
    compiled = [_compile_pattern(p) for p in patterns]
    return tuple(entry for entry in self.entries if any(p.fullmatch(entry.module) for p in compiled))


def build_exception_trail(exc: BaseException, *, walk_chain: bool = True) -> ExceptionTrail:
  """Build the full origin trail for *exc*.

  Args:
    exc: The exception to walk. Must have a live ``__traceback__`` (i.e. called from inside the
      ``except`` block that caught it, or with a manually-attached traceback).
    walk_chain: When ``True`` (default), also walks ``__cause__``/``__context__`` recursively, both
      links at every node (not just whichever is set), with ``id()``-based cycle detection. Matches
      the app-side ``_is_database_origin_exception`` behavior this trail replaces. See
      ``ExceptionTrail.ambiguous_ancestry`` for the one case this can't order from the objects alone.

  Returns:
    An ``ExceptionTrail`` whose ``entries`` is never empty -- an exception always has at least one
    frame, the one that raised it.

  Raises:
    ValueError: *exc* has no live ``__traceback__`` (e.g. it was constructed but never raised).
  """
  if exc.__traceback__ is None:
    raise ValueError(f"build_exception_trail requires a live traceback -- {exc!r} was never raised")
  entries, ambiguous_ancestry = _build_entries(exc, walk_chain=walk_chain)
  first_party = next((e for e in entries if e.category is OriginCategory.FIRST_PARTY), None)
  return ExceptionTrail(
    entries=entries, origin=entries[0], first_party_entry=first_party, ambiguous_ancestry=ambiguous_ancestry
  )
