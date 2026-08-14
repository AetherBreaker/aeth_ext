"""Standardized fatal-exception-origin trail (replaces `extract_details_callable`, TODO.md #6).

`build_exception_trail` walks an exception's traceback (and, by default, its `__cause__`/`__context__`
chain) into an `ExceptionTrail`: an ordered, deduplicated, origin-first sequence of `TrailEntry`
records, each categorized as first-party, third-party, stdlib, or unpackaged. `ExceptionTrail.matches`
is the one standardized way to ask "did this failure touch marked module(s)" -- replacing every
consumer's own hand-rolled frame-walk/path-match logic.
"""

# Standard library imports
import re
import sys
from dataclasses import dataclass
from enum import auto
from os.path import isfile, join
from typing import TYPE_CHECKING, NamedTuple

# First party imports
from aeth_ext.static_eval import get_entrypoint_root, get_package_root
from aeth_ext.types import StrEnum

if TYPE_CHECKING:
  # Standard library imports
  from types import FrameType

__all__ = ["ExceptionTrail", "OriginCategory", "TrailEntry", "build_exception_trail"]


class OriginCategory(StrEnum):
  """Where a `TrailEntry`'s module lives, relative to the running application."""

  FIRST_PARTY = auto()
  """Belongs to the application that is running (its package root matches `get_entrypoint_root()`)."""

  THIRD_PARTY = auto()
  """An installed dependency, or a resolvable package that is not the host application's own."""

  STDLIB = auto()
  """Part of the Python standard library (`sys.stdlib_module_names`)."""

  UNPACKAGED = auto()
  """No resolvable package root -- a loose script, `__main__`, or code with no real backing file."""


class TrailEntry(NamedTuple):
  """One distinct module transition in an `ExceptionTrail`, origin-first."""

  module: str
  category: OriginCategory
  file: str


_SEGMENT_STAR = "*"
_SEGMENT_DOUBLE_STAR = "**"


def _compile_pattern(pattern: str) -> re.Pattern[str]:
  """Compile a dot-segment glob *pattern* into a fully-anchored regex.

  `*` matches exactly one segment; `**` matches zero or more segments, including the separating dot on
  whichever side has a neighboring segment -- this is what lets `"a.**.b"` match the zero-segment case
  `"a.b"`, not just `"a.x.b"`. A `**` with a segment on only one side swallows only that side's dot; a
  bare `"**"` matches any non-empty dotted name. Not cached: patterns are typically static constants at
  a call site, so compiling per `ExceptionTrail.matches()` call costs nothing worth caching against.
  """
  segments = pattern.split(".")
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


def _categorize(module: str, file: str) -> OriginCategory:
  """Categorize a single resolved `(module, file)` pair.

  Order matters: stdlib is checked first (a stdlib frame has no meaningful package root to
  compute), then the file's own package root decides third-party/first-party/unpackaged.
  """
  top_level = module.partition(".")[0]
  if top_level in sys.stdlib_module_names:
    return OriginCategory.STDLIB

  root = get_package_root(file)
  if "site-packages" in root.replace("\\", "/").split("/"):
    return OriginCategory.THIRD_PARTY
  if not isfile(join(root, "__init__.py")):
    return OriginCategory.UNPACKAGED
  return OriginCategory.FIRST_PARTY if root == get_entrypoint_root() else OriginCategory.THIRD_PARTY


def _resolve_frame(frame: FrameType) -> tuple[str, str] | None:
  """Return `(module, file)` for *frame*, or `None` if it has no usable module/file at all."""
  module = frame.f_globals.get("__name__")
  file = frame.f_code.co_filename
  if not module or not file or not isfile(file):
    return None
  return module, file


def _frames_innermost_first(exc: BaseException) -> list[FrameType]:
  """Every frame on `exc.__traceback__`, innermost (where it was raised) first."""
  frames: list[FrameType] = []
  tb = exc.__traceback__
  while tb is not None:
    frames.append(tb.tb_frame)
    tb = tb.tb_next
  frames.reverse()
  return frames


def _chain_root_first(exc: BaseException) -> list[BaseException]:
  """*exc* and its `__cause__`/`__context__` ancestors, oldest cause first, `exc` last.

  `id()`-based cycle detection: an implicit `__context__` loop stops the walk instead of hanging.
  """
  seen: set[int] = set()
  chain: list[BaseException] = []
  current: BaseException | None = exc
  while current is not None and id(current) not in seen:
    seen.add(id(current))
    chain.append(current)
    current = current.__cause__ or current.__context__
  chain.reverse()
  return chain


def _build_entries(exc: BaseException, *, walk_chain: bool) -> tuple[TrailEntry, ...]:
  """Walk *exc* (and optionally its cause chain) into deduplicated, origin-first `TrailEntry` tuples."""
  exceptions = _chain_root_first(exc) if walk_chain else [exc]

  entries: list[TrailEntry] = []
  last_module: str | None = None
  for one_exc in exceptions:
    for frame in _frames_innermost_first(one_exc):
      resolved = _resolve_frame(frame)
      if resolved is None:
        module, file = "<unknown>", frame.f_code.co_filename or "<unknown>"
        category = OriginCategory.UNPACKAGED
      else:
        module, file = resolved
        category = _categorize(module, file)
      if module == last_module:
        continue
      entries.append(TrailEntry(module=module, category=category, file=file))
      last_module = module

  return tuple(entries)


@dataclass(frozen=True, slots=True)
class ExceptionTrail:
  """A single fatal exception's full origin trail, origin-first and deduplicated.

  Built once by `build_exception_trail` and never mutated -- every attribute below is computed
  eagerly at construction, not lazily, since the walk itself already dominates the cost.
  """

  entries: tuple[TrailEntry, ...]
  origin: TrailEntry
  first_party_entry: TrailEntry | None

  def matches(self, *patterns: str) -> tuple[TrailEntry, ...]:
    """Every entry whose module matches any of *patterns* (see `_compile_pattern` for glob syntax).

    Returns entries in trail order (origin-first). The empty tuple is falsy, so
    `if trail.matches(...):` reads as a boolean check while still handing back full match detail to a
    caller that wants it.
    """
    compiled = [_compile_pattern(p) for p in patterns]
    return tuple(entry for entry in self.entries if any(p.fullmatch(entry.module) for p in compiled))


def build_exception_trail(exc: BaseException, *, walk_chain: bool = True) -> ExceptionTrail:
  """Build the full origin trail for *exc*.

  Args:
    exc: The exception to walk. Must have a live `__traceback__` (i.e. called from inside the
      `except` block that caught it, or with a manually-attached traceback).
    walk_chain: When `True` (default), also walks `__cause__`/`__context__` recursively, with
      `id()`-based cycle detection. Matches the app-side `_is_database_origin_exception` behavior
      this trail replaces.

  Returns:
    An `ExceptionTrail` whose `entries` is never empty -- an exception always has at least one
    frame, the one that raised it.
  """
  entries = _build_entries(exc, walk_chain=walk_chain)
  first_party = next((e for e in entries if e.category is OriginCategory.FIRST_PARTY), None)
  return ExceptionTrail(entries=entries, origin=entries[0], first_party_entry=first_party)
